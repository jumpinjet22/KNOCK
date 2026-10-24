"""A doorbell ring nobody speaks on (issue #50)."""

from datetime import UTC, datetime

from knock.core.events import VisitorEvent
from knock.core.orchestrator import SILENT_VISITOR_REPLY, Orchestrator
from knock.core.state import SessionState


class _ExplodingLLM:
    """Any model call on a silent ring is a bug."""

    def generate(self, prompt: str) -> str:
        raise AssertionError("the model must not be asked about a silent ring")


def _event(text: str = "Doorbell ring on 696ab4810067bb03e400a1fc") -> VisitorEvent:
    return VisitorEvent(source="unifi-x", text=text, timestamp=datetime.now(UTC))


def test_silent_visitor_gets_the_fixed_neutral_prompt_without_the_model() -> None:
    state = SessionState(session_id="s")
    orchestrator = Orchestrator(llm_provider=_ExplodingLLM())  # type: ignore[arg-type]

    decision = orchestrator.respond(
        _event(), state=state, suppress_greeting=True, silent_visitor=True
    )

    assert decision.text == SILENT_VISITOR_REPLY
    assert decision.intent == "unknown"
    assert decision.escalate is False
    assert "696ab481" not in decision.text
    assert state.turn_count == 1


def test_a_silent_ring_is_not_classified_even_if_the_placeholder_looks_like_a_delivery() -> None:
    decision = Orchestrator().respond(
        _event("Doorbell ring on cam1 (camera also shows: a package and a delivery uniform)"),
        silent_visitor=True,
    )
    assert decision.text == SILENT_VISITOR_REPLY


def test_real_speech_is_unaffected() -> None:
    decision = Orchestrator().respond(_event("Hi, I have a package"), silent_visitor=False)
    assert "leave the package" in decision.text.lower()
