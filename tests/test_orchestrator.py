from datetime import UTC, datetime

from knock.core.audit import AuditEntry
from knock.core.events import VisitorEvent
from knock.core.orchestrator import Orchestrator


def _event(text: str) -> VisitorEvent:
    return VisitorEvent(source="test", text=text, timestamp=datetime.now(UTC))


class _FakeAuditLog:
    def __init__(self) -> None:
        self.entries: list[AuditEntry] = []

    def record(self, entry: AuditEntry) -> None:
        self.entries.append(entry)


def test_delivery_intent() -> None:
    decision = Orchestrator().respond(_event("Hi, I have an Amazon package"))
    assert "leave the package" in decision.text.lower()
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
