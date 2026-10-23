from datetime import UTC, datetime, timedelta

import pytest

from knock.core.audit import NullAuditLog
from knock.core.events import VisitorEvent
from knock.core.one_shot import answer_once
from knock.core.orchestrator import Orchestrator
from knock.core.session_ids import build_session_id
from knock.core.session_store import JSONFileSessionStore
from knock.core.state import SessionState
from knock.integrations.frigate import _default_session_id as frigate_id
from knock.integrations.homeassistant import _default_session_id as ha_id
from knock.integrations.mqtt import _default_session_id as mqtt_id
from knock.integrations.unifi import _default_session_id as unifi_id


@pytest.mark.parametrize("raw", ["cam1", "a/b", "", "../x", "é cam", "x" * 300, "ok_-9"])
def test_every_connector_builds_its_session_ids_the_same_way(raw) -> None:
    for prefix, build in (
        ("frigate", frigate_id),
        ("ha", ha_id),
        ("mqtt", mqtt_id),
        ("unifi", unifi_id),
    ):
        assert build(raw) == build_session_id(prefix, raw)


def test_a_session_id_can_never_carry_a_path_separator_or_exceed_128_characters() -> None:
    built = build_session_id("ha", "../../etc/passwd" + "x" * 300)
    assert "/" not in built and ".." not in built
    assert len(built) == 128
    assert build_session_id("ha", "") == "ha-unknown"


def test_answer_once_resumes_the_stored_session_and_saves_it(tmp_path) -> None:
    store = JSONFileSessionStore(tmp_path)
    store.save(
        SessionState(
            session_id="s1", turn_count=3, updated_at=datetime.now(UTC) - timedelta(hours=3)
        )
    )
    event = VisitorEvent(source="x", text="hello", timestamp=datetime.now(UTC))

    answer_once(Orchestrator(), store, NullAuditLog(), "s1", event)

    state = store.load("s1")
    assert state is not None and state.turn_count == 4


def test_only_letters_digits_underscore_and_dash_survive() -> None:
    assert build_session_id("ha", "a_b-C9") == "ha-a_b-C9"
    assert build_session_id("ha", "a.b/c d") == "ha-a_b_c_d"
