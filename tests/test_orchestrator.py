from datetime import UTC, datetime

from knock.core.audit import AuditEntry
from knock.core.events import VisitorEvent
from knock.core.orchestrator import Orchestrator
from knock.core.state import SessionState


def _event(text: str) -> VisitorEvent:
    return VisitorEvent(source="test", text=text, timestamp=datetime.now(UTC))


def _new_state(session_id: str = "s1") -> SessionState:
    return SessionState(session_id=session_id, updated_at=datetime.now(UTC))


class _FakeAuditLog:
    def __init__(self) -> None:
        self.entries: list[AuditEntry] = []

    def record(self, entry: AuditEntry) -> None:
        self.entries.append(entry)


def test_delivery_intent() -> None:
    decision = Orchestrator().respond(_event("Hi, I have an Amazon package"))
    assert "leave the package" in decision.text.lower()
    assert decision.escalate is False


def test_response_decision_carries_the_classified_intent() -> None:
    # Lets a caller (e.g. UnifiBridge notifying Home Assistant on a
    # signature-required delivery) react to a specific situation without
    # re-deriving the intent itself.
    assert Orchestrator().respond(_event("Hi, I have an Amazon package")).intent == "delivery"
    assert Orchestrator().respond(_event("Fire emergency, help!")).intent == "emergency"
    assert Orchestrator().respond(_event("Is anyone home right now?")).intent == "blocked_request"


def test_delivery_signature_required_intent() -> None:
    decision = Orchestrator().respond(_event("I have a package that needs a signature"))
    assert "homeowner" in decision.text.lower()
    assert "leave the package" not in decision.text.lower()
    assert decision.escalate is False


def test_food_delivery_intent() -> None:
    decision = Orchestrator().respond(_event("Hi I have a pizza delivery"))
    assert "right away" in decision.text.lower()
    assert "leave the package" not in decision.text.lower()
    assert decision.escalate is False
    assert decision.intent == "food_delivery"


def test_religious_soliciting_intent() -> None:
    decision = Orchestrator().respond(_event("Do you have a minute to talk about the Bible?"))
    assert "not interested" in decision.text.lower()
    assert decision.escalate is False


def test_political_soliciting_intent() -> None:
    decision = Orchestrator().respond(_event("I'm here for the campaign, can I get your vote?"))
    assert "politics" in decision.text.lower()
    assert decision.escalate is False


def test_general_soliciting_intent() -> None:
    decision = Orchestrator().respond(_event("I'm selling magazine subscriptions door-to-door"))
    assert "solicitations" in decision.text.lower()
    assert decision.escalate is False


def test_unknown_visitor_fallback() -> None:
    decision = Orchestrator().respond(_event("Do you like jazz?"))
    assert "can't help" in decision.text.lower()


class _FakeLLMProvider:
    name = "fake-llm"

    def __init__(self, response: str = "Sorry, I'm not sure how to help with that.") -> None:
        self.response = response
        self.prompts: list[str] = []

    def generate(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.response


class _SequencedLLMProvider:
    """Returns a different canned reply per call, in order -- for testing
    the two-call classify-then-phrase flow where each call needs its own
    response (unlike `_FakeLLMProvider`'s single fixed reply).
    """

    name = "sequenced-fake-llm"

    def __init__(self, responses: list[str]) -> None:
        self._responses = iter(responses)
        self.prompts: list[str] = []

    def generate(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return next(self._responses)


class _FailingLLMProvider:
    name = "failing-llm"

    def generate(self, prompt: str) -> str:
        raise RuntimeError("ollama is down")


def test_unknown_intent_uses_the_llm_fallback_when_configured() -> None:
    # A fixed-reply fake also stands in for "the classification step found
    # no matching label" (its reply isn't one of the known intents), so
    # this still ends up phrasing as "unknown" -- just via two LLM calls
    # now (classify, then phrase) instead of one.
    llm = _FakeLLMProvider("Sorry, could you repeat that?")
    decision = Orchestrator(llm_provider=llm).respond(_event("Do you like jazz?"))

    assert decision.text == "Sorry, could you repeat that?"
    assert len(llm.prompts) == 2
    assert all("Do you like jazz?" in prompt for prompt in llm.prompts)


def test_llm_is_consulted_for_a_known_intent_too_not_just_unknown() -> None:
    # The whole point of wiring an LLM in is to phrase responses using what
    # was actually said/seen instead of reciting the same fixed string for
    # every delivery -- it must not be limited to the unknown bucket.
    llm = _FakeLLMProvider("Sure, go ahead and leave it by the door, thanks!")
    decision = Orchestrator(llm_provider=llm).respond(_event("Hi, I have an Amazon package"))

    assert decision.text == "Sure, go ahead and leave it by the door, thanks!"
    assert len(llm.prompts) == 1
    assert "Hi, I have an Amazon package" in llm.prompts[0]
    assert "delivery" in llm.prompts[0].lower()


def test_llm_prompt_for_food_delivery_warns_against_leaving_it_at_the_door() -> None:
    llm = _FakeLLMProvider("Sure, I'll let them know right away.")
    Orchestrator(llm_provider=llm).respond(_event("Hi I have a pizza delivery"))

    assert len(llm.prompts) == 1
    assert "food" in llm.prompts[0].lower()
    assert "leave it by the door" in llm.prompts[0].lower()


def test_known_intent_uses_the_canned_response_without_an_llm_provider() -> None:
    decision = Orchestrator().respond(_event("Hi, I have an Amazon package"))
    assert "leave the package" in decision.text.lower()


def test_llm_fallback_failure_falls_back_to_the_canned_response() -> None:
    decision = Orchestrator(llm_provider=_FailingLLMProvider()).respond(_event("Do you like jazz?"))
    assert "can't help" in decision.text.lower()


def test_llm_failure_on_a_known_intent_falls_back_to_its_own_canned_response() -> None:
    decision = Orchestrator(llm_provider=_FailingLLMProvider()).respond(
        _event("Hi, I have an Amazon package")
    )
    assert "leave the package" in decision.text.lower()


def test_llm_fallback_blank_response_falls_back_to_the_canned_response() -> None:
    decision = Orchestrator(llm_provider=_FakeLLMProvider("   ")).respond(
        _event("Do you like jazz?")
    )
    assert "can't help" in decision.text.lower()


def test_llm_fallback_response_is_still_style_truncated() -> None:
    llm = _FakeLLMProvider("x" * 500)
    decision = Orchestrator(llm_provider=llm).respond(_event("Do you like jazz?"))
    assert len(decision.text) <= 140


def test_llm_fallback_is_never_consulted_for_a_blocked_request() -> None:
    llm = _FakeLLMProvider()
    Orchestrator(llm_provider=llm).respond(_event("Is anyone home right now?"))
    assert llm.prompts == []


def test_llm_fallback_is_never_consulted_for_an_emergency() -> None:
    llm = _FakeLLMProvider()
    Orchestrator(llm_provider=llm).respond(_event("Fire emergency, help!"))
    assert llm.prompts == []


def test_emergency_escalation() -> None:
    decision = Orchestrator().respond(_event("Fire emergency, help!"))
    assert decision.escalate is True
    assert decision.reason == "emergency"


def test_occupancy_question_blocked() -> None:
    decision = Orchestrator().respond(_event("Is anyone home right now?"))
    assert decision.reason == "blocked_request"
    assert "can't share" in decision.text.lower()


def test_respond_records_an_audit_entry() -> None:
    audit_log = _FakeAuditLog()
    Orchestrator(audit_log=audit_log).respond(_event("Hi, I have an Amazon package"))

    assert len(audit_log.entries) == 1
    entry = audit_log.entries[0]
    assert entry.text == "Hi, I have an Amazon package"
    assert entry.allowed is True
    assert entry.reason == "normal"
    assert entry.intent == "delivery"


def test_respond_accepts_a_per_call_audit_log_override() -> None:
    default_log = _FakeAuditLog()
    override_log = _FakeAuditLog()
    orchestrator = Orchestrator(audit_log=default_log)

    orchestrator.respond(_event("Fire emergency, help!"), audit_log=override_log)

    assert len(override_log.entries) == 1
    assert len(default_log.entries) == 0
    assert override_log.entries[0].reason == "emergency"


def test_respond_without_a_session_never_greets() -> None:
    decision = Orchestrator().respond(_event("Hi, I have an Amazon package"))
    assert "my name is knock" not in decision.text.lower()


def test_respond_greets_on_a_sessions_first_normal_turn() -> None:
    state = _new_state()
    decision = Orchestrator().respond(_event("Hi, I have an Amazon package"), state=state)
    assert "my name is knock" in decision.text.lower()
    assert "leave the package" in decision.text.lower()


def test_respond_suppresses_greeting_when_caller_already_said_it_aloud() -> None:
    state = _new_state()
    decision = Orchestrator().respond(
        _event("Hi, I have an Amazon package"), state=state, suppress_greeting=True
    )
    assert "my name is knock" not in decision.text.lower()
    assert "leave the package" in decision.text.lower()


def test_respond_does_not_greet_on_later_turns() -> None:
    state = _new_state()
    orchestrator = Orchestrator()

    first = orchestrator.respond(_event("Hi, I have an Amazon package"), state=state)
    second = orchestrator.respond(_event("Hi, I have an Amazon package"), state=state)

    assert "my name is knock" in first.text.lower()
    assert "my name is knock" not in second.text.lower()


def test_respond_greets_on_a_sessions_first_blocked_turn() -> None:
    state = _new_state()
    decision = Orchestrator().respond(_event("Is anyone home right now?"), state=state)
    assert "my name is knock" in decision.text.lower()
    assert "can't share" in decision.text.lower()


def test_respond_never_greets_on_emergency() -> None:
    state = _new_state()
    decision = Orchestrator().respond(_event("Fire emergency, help!"), state=state)
    assert "my name is knock" not in decision.text.lower()


def test_respond_does_not_repeat_greeting_across_repeated_blocked_turns() -> None:
    state = _new_state()
    orchestrator = Orchestrator()

    first = orchestrator.respond(_event("Is anyone home right now?"), state=state)
    second = orchestrator.respond(_event("Is anyone home right now?"), state=state)

    assert "my name is knock" in first.text.lower()
    assert "my name is knock" not in second.text.lower()
    assert state.turn_count == 2
    assert state.history == ["Is anyone home right now?", "Is anyone home right now?"]


def test_respond_advances_turn_count_and_history_on_a_blocked_turn() -> None:
    state = _new_state()
    Orchestrator().respond(_event("Is anyone home right now?"), state=state)

    assert state.turn_count == 1
    assert state.last_intent == "blocked_request"
    assert state.history == ["Is anyone home right now?"]


def test_respond_advances_turn_count_and_history_on_an_emergency_turn() -> None:
    state = _new_state()
    Orchestrator().respond(_event("Fire emergency, help!"), state=state)

    assert state.turn_count == 1
    assert state.last_intent == "emergency"
    assert state.history == ["Fire emergency, help!"]


def test_respond_does_not_regreet_after_an_emergency_turn_is_followed_by_a_normal_one() -> None:
    state = _new_state()
    orchestrator = Orchestrator()

    orchestrator.respond(_event("Fire emergency, help!"), state=state)
    second = orchestrator.respond(_event("Hi, I have an Amazon package"), state=state)

    assert "my name is knock" not in second.text.lower()


# -- summarize_for_notification ----------------------------------------------------


def test_summarize_for_notification_uses_the_llm_when_configured() -> None:
    llm = _FakeLLMProvider("A FedEx driver has a package for you")
    summary = Orchestrator(llm_provider=llm).summarize_for_notification(
        "FedEx driver with a box", fallback="A delivery needs a signature."
    )

    assert summary == "A FedEx driver has a package for you"
    assert len(llm.prompts) == 1
    assert "FedEx driver with a box" in llm.prompts[0]


def test_summarize_for_notification_falls_back_without_a_provider() -> None:
    summary = Orchestrator().summarize_for_notification(
        "FedEx driver with a box", fallback="A delivery needs a signature."
    )
    assert summary == "A delivery needs a signature."


def test_summarize_for_notification_falls_back_on_llm_failure() -> None:
    summary = Orchestrator(llm_provider=_FailingLLMProvider()).summarize_for_notification(
        "FedEx driver with a box", fallback="A delivery needs a signature."
    )
    assert summary == "A delivery needs a signature."


def test_summarize_for_notification_falls_back_on_a_blank_llm_reply() -> None:
    summary = Orchestrator(llm_provider=_FakeLLMProvider("   ")).summarize_for_notification(
        "FedEx driver with a box", fallback="A delivery needs a signature."
    )
    assert summary == "A delivery needs a signature."


# -- LLM-refined "unknown" intents (service_appointment, person_lookup, etc) ------


def test_unknown_message_gets_refined_by_the_llm_into_a_specific_intent() -> None:
    llm = _SequencedLLMProvider(
        ["service_appointment", "Thanks, I'll let them know you're here for your appointment."]
    )
    decision = Orchestrator(llm_provider=llm).respond(
        _event("I'm here to fix the water heater, I have an appointment")
    )

    assert decision.intent == "service_appointment"
    assert len(llm.prompts) == 2
    assert "Pick the single" not in llm.prompts[0]  # sanity: not asserting exact wording
    assert decision.text == "Thanks, I'll let them know you're here for your appointment."


def test_llm_refinement_is_never_consulted_when_keywords_already_matched() -> None:
    # Only the phrasing call happens (one prompt) -- the classification
    # call is skipped entirely since classify_intent() already found a
    # match, not just "called but ignored."
    llm = _SequencedLLMProvider(["Sure, go ahead and leave it by the door."])
    decision = Orchestrator(llm_provider=llm).respond(_event("Hi, I have an Amazon package"))

    assert decision.intent == "delivery"
    assert len(llm.prompts) == 1
    assert "keyword classifier guesses" in llm.prompts[0].lower()


def test_llm_refinement_falls_back_to_unknown_on_an_unrecognized_label() -> None:
    llm = _SequencedLLMProvider(["something-made-up", "Sorry, I can't help with that right now."])
    decision = Orchestrator(llm_provider=llm).respond(_event("Do you like jazz?"))

    assert decision.intent == "unknown"


def test_llm_refinement_falls_back_to_unknown_without_a_provider() -> None:
    decision = Orchestrator().respond(_event("Do you like jazz?"))
    assert decision.intent == "unknown"


def test_llm_refinement_falls_back_to_unknown_on_failure() -> None:
    decision = Orchestrator(llm_provider=_FailingLLMProvider()).respond(_event("Do you like jazz?"))
    assert decision.intent == "unknown"
    assert "can't help" in decision.text.lower()


def test_person_lookup_intent() -> None:
    llm = _SequencedLLMProvider(["person_lookup", "I'll pass along that you're looking for them."])
    decision = Orchestrator(llm_provider=llm).respond(_event("Is John here?"))
    assert decision.intent == "person_lookup"
    assert decision.text == "I'll pass along that you're looking for them."


def test_official_visit_intent() -> None:
    llm = _SequencedLLMProvider(
        ["official_visit", "I'll make sure the household is aware you're here."]
    )
    decision = Orchestrator(llm_provider=llm).respond(
        _event("I'm here from the city inspector's office")
    )
    assert decision.intent == "official_visit"


def test_suspicious_activity_intent() -> None:
    llm = _SequencedLLMProvider(["suspicious_activity", "I've let the household know you're here."])
    decision = Orchestrator(llm_provider=llm).respond(_event("Just checking out the property"))
    assert decision.intent == "suspicious_activity"


def test_ride_arrived_intent_via_keyword() -> None:
    # "uber" is a keyword match, so this never consults the LLM at all.
    decision = Orchestrator().respond(_event("Your Uber is here"))
    assert decision.intent == "ride_arrived"
    assert "ride" in decision.text.lower()


def test_visitation_intent_via_llm_refinement() -> None:
    # No keyword list for this one at all -- recognizing a casual social
    # visit depends entirely on the LLM refinement step.
    llm = _SequencedLLMProvider(["visitation", "Thanks, I'll let them know you're here!"])
    decision = Orchestrator(llm_provider=llm).respond(_event("Hey it's me, just came by to say hi"))
    assert decision.intent == "visitation"


def test_llm_refinement_recovers_a_delivery_phrasing_keywords_missed() -> None:
    # The actual production bug: "I came to drop some food off" matches no
    # food/delivery keyword, so classify_intent() alone would call it
    # "unknown" forever -- the LLM refinement step is the safety net that
    # must still land on the right real intent, not just the 4 categories
    # with no keyword list at all.
    llm = _SequencedLLMProvider(["food_delivery", "Thanks, I'll let them know right away."])
    decision = Orchestrator(llm_provider=llm).respond(_event("I came to drop some food off"))
    assert decision.intent == "food_delivery"


def test_llm_refinement_recovers_soliciting_phrasing_keywords_missed() -> None:
    llm = _SequencedLLMProvider(["soliciting", "Sorry, we don't accept solicitations here."])
    decision = Orchestrator(llm_provider=llm).respond(
        _event("Got a minute to hear about our lawn care service?")
    )
    assert decision.intent == "soliciting"
