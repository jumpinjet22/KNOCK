from datetime import UTC, datetime

from knock.core.state import SessionState


def test_session_state_defaults() -> None:
    state = SessionState(session_id="abc", updated_at=datetime.now(UTC))
    assert state.turn_count == 0
    assert state.last_intent == "unknown"
    assert state.history == []
