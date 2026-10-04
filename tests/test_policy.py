from knock.conversation.policy import PolicyEngine


def test_emergency_escalation() -> None:
    decision = PolicyEngine().evaluate("Help, medical emergency")
    assert decision.allowed is True
    assert decision.reason == "emergency"
    assert "emergency" in decision.flags
    assert decision.confidence["emergency"] >= 1.0
    assert decision.matched_rule_ids


def test_occupancy_blocking() -> None:
    decision = PolicyEngine().evaluate("Are you home right now?")
    assert decision.allowed is False
    assert decision.reason == "blocked_request"
    assert decision.flags == ["occupancy"]


def test_schedule_probe_is_blocked() -> None:
    decision = PolicyEngine().evaluate("What's your schedule like this week?")
    assert decision.allowed is False
    assert decision.reason == "blocked_request"
    assert decision.flags == ["schedule"]


def test_the_word_scheduled_does_not_false_positive_the_schedule_block() -> None:
    # Found via live testing: the schedule rule's phrase used to be the bare
    # word "schedule", which matched as a substring inside "scheduled" --
    # so a technician saying "here for the scheduled furnace tune-up" got
    # hard-blocked with "Sorry, I can't share that information" instead of
    # being recognized as a normal service_appointment. The rule phrase is
    # "your schedule" now, specific enough not to collide with "scheduled".
    decision = PolicyEngine().evaluate("Here for the scheduled furnace tune-up")
    assert decision.allowed is True
    assert decision.reason == "normal"
    assert decision.flags == []


def test_normal_text_is_allowed_with_no_flags() -> None:
    decision = PolicyEngine().evaluate("Hi, I have a package for you")
    assert decision.allowed is True
    assert decision.reason == "normal"
    assert decision.flags == []
    assert decision.matched_rule_ids == []


# -- apply_style's entry-invitation backstop ---------------------------------
#
# Found via adversarial testing: a visitor phrasing an entry request in a
# way that dodges PolicyEngine's exact "let me in" rule (e.g. "mind letting
# me inside?") reaches the LLM response-phrasing path, where the model
# sometimes actually replied with an invitation ("feel free to come in!")
# despite the prompt instruction against it. This is the deterministic
# backstop -- same pattern as providers/vision/safety.py's alarming-language
# filter -- that catches it regardless of what the model said.


def test_apply_style_suppresses_an_entry_invitation() -> None:
    styled = PolicyEngine().apply_style("Sure, feel free to come in for a moment!")
    assert styled == "Thanks, I'll pass that along."


def test_apply_style_catches_several_invitation_phrasings() -> None:
    engine = PolicyEngine()
    for phrase in [
        "Come on in, make yourself at home.",
        "You can enter through the side door.",
        "I'll let you in now.",
        "Head on in, it's unlocked.",
    ]:
        assert engine.apply_style(phrase) == "Thanks, I'll pass that along."


def test_apply_style_leaves_a_normal_response_unchanged() -> None:
    styled = PolicyEngine().apply_style("Thanks, I'll pass that along.")
    assert styled == "Thanks, I'll pass that along."


def test_apply_style_still_truncates_to_140_chars() -> None:
    styled = PolicyEngine().apply_style("x" * 200)
    assert len(styled) == 140
