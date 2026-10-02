import time
from datetime import UTC, datetime

import pytest

from knock.core.session_store import JSONFileSessionStore
from knock.core.state import SessionState


def test_round_trips_session_state(tmp_path) -> None:
    store = JSONFileSessionStore(tmp_path)
    state = SessionState(
        session_id="abc123",
        turn_count=2,
        last_intent="delivery",
        updated_at=datetime.now(UTC),
        history=["hi", "package"],
    )

    store.save(state)
    loaded = store.load("abc123")

    assert loaded is not None
    assert loaded.session_id == "abc123"
    assert loaded.turn_count == 2
    assert loaded.last_intent == "delivery"
    assert loaded.history == ["hi", "package"]


def test_load_missing_session_returns_none(tmp_path) -> None:
    store = JSONFileSessionStore(tmp_path)
    assert store.load("does-not-exist") is None


def test_creates_directory_if_missing(tmp_path) -> None:
    nested = tmp_path / "nested" / "sessions"
    store = JSONFileSessionStore(nested)
    assert nested.exists()
    assert store.load("anything") is None


@pytest.mark.parametrize("bad_id", ["../escape", "a/b", "", "x" * 200])
def test_rejects_unsafe_session_ids(tmp_path, bad_id: str) -> None:
    store = JSONFileSessionStore(tmp_path)
    with pytest.raises(ValueError):
        store.load(bad_id)


def test_list_ids_is_empty_for_a_fresh_store(tmp_path) -> None:
    store = JSONFileSessionStore(tmp_path)
    assert store.list_ids() == []


def test_list_ids_returns_most_recently_updated_first(tmp_path) -> None:
    store = JSONFileSessionStore(tmp_path)
    store.save(SessionState(session_id="first"))
    time.sleep(0.01)  # ensure a distinct mtime from "first"
    store.save(SessionState(session_id="second"))

    assert store.list_ids() == ["second", "first"]
