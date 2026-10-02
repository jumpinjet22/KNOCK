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


def test_delivery_signature_required_intent() -> None:
    decision = Orchestrator().respond(_event("I have a package that needs a signature"))
    assert "sign" in decision.text.lower()
    assert "leave the package" not in decision.text.lower()
    assert decision.escalate is False


def test_unknown_visitor_fallback() -> None:
    decision = Orchestrator().respond(_event("Do you like jazz?"))
    assert "can't help" in decision.text.lower()


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
