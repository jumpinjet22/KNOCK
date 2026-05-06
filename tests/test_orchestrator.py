from datetime import UTC, datetime

from knock.core.events import VisitorEvent
from knock.core.orchestrator import Orchestrator


def _event(text: str) -> VisitorEvent:
    return VisitorEvent(source="test", text=text, timestamp=datetime.now(UTC))


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
