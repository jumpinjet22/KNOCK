"""Pins how the Frigate and Home Assistant bridges answer a trigger today,
before their shared plumbing moves into core (plans/unifi-split.md, step 4).

Both are one-shot: load (or start) the camera's/entity's session, ask the
orchestrator once, save. Unlike a UniFi visit there is no greeting spoken
separately, no idle reset of the session and no conversation loop -- so
they must NOT be moved onto `VisitRunner`, and these tests say why.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import httpx
import respx

from knock.config import FrigateConfig, HomeAssistantConfig
from knock.core.audit import JSONLAuditLog
from knock.core.orchestrator import Orchestrator
from knock.core.session_store import JSONFileSessionStore
from knock.core.state import SessionState
from knock.integrations.frigate import FrigateBridge, FrigateDetection
from knock.integrations.homeassistant import HomeAssistantBridge

_HOURS_AGO = datetime.now(UTC) - timedelta(hours=3)


def _frigate(tmp_path) -> FrigateBridge:
    return FrigateBridge(
        config=FrigateConfig(http_host="127.0.0.1", http_port=5000),
        session_store=JSONFileSessionStore(tmp_path),
        audit_log=JSONLAuditLog(tmp_path / "audit.jsonl"),
    )


def _detection(has_snapshot: bool = False) -> FrigateDetection:
    return FrigateDetection(
        event_id="evt-1",
        camera="front_door",
        label="person",
        zones=("front_porch",),
        has_snapshot=has_snapshot,
    )


def _ha(tmp_path) -> HomeAssistantBridge:
    return HomeAssistantBridge(
        config=HomeAssistantConfig(
            base_url="http://ha.local:8123",
            token="t",
            trigger_entity_id="binary_sensor.front_doorbell",
        ),
        session_store=JSONFileSessionStore(tmp_path),
        audit_log=JSONLAuditLog(tmp_path / "audit.jsonl"),
    )


_HA_EVENT = {
    "entity_id": "binary_sensor.front_doorbell",
    "old_state": {"state": "off"},
    "new_state": {"state": "on"},
}


def _recording(bridge: Any) -> list[dict[str, Any]]:
    """Wrap the bridge's orchestrator so each respond() call's arguments
    are captured while the real one still runs.
    """
    calls: list[dict[str, Any]] = []
    orchestrator = Orchestrator()
    real = orchestrator.respond

    def respond(event, state=None, audit_log=None, **kwargs):
        calls.append({"event": event, "state": state, "kwargs": kwargs})
        return real(event, state=state, audit_log=audit_log, **kwargs)

    orchestrator.respond = respond  # type: ignore[method-assign]
    bridge.orchestrator = orchestrator
    return calls


# -- sessions never reset on idle ----------------------------------------------------------


def test_frigate_resumes_an_old_session_however_long_ago_it_was(tmp_path) -> None:
    bridge = _frigate(tmp_path)
    bridge.session_store.save(
        SessionState(session_id="frigate-front_door", turn_count=3, updated_at=_HOURS_AGO)
    )

    bridge.handle_detection(_detection())

    state = bridge.session_store.load("frigate-front_door")
    assert state is not None and state.turn_count == 4


def test_home_assistant_resumes_an_old_session_however_long_ago_it_was(tmp_path) -> None:
    bridge = _ha(tmp_path)
    bridge.session_store.save(
        SessionState(
            session_id="ha-binary_sensor_front_doorbell", turn_count=3, updated_at=_HOURS_AGO
        )
    )

    bridge.handle_state_changed(_HA_EVENT)

    state = bridge.session_store.load("ha-binary_sensor_front_doorbell")
    assert state is not None and state.turn_count == 4


# -- one orchestrator call, with today's arguments -----------------------------------------


def test_frigate_asks_the_orchestrator_once_without_suppressing_the_greeting(tmp_path) -> None:
    bridge = _frigate(tmp_path)
    calls = _recording(bridge)

    bridge.handle_detection(_detection())

    assert len(calls) == 1
    assert calls[0]["kwargs"] == {"image": None}
    assert calls[0]["event"].source == "frigate-front_door"
    assert calls[0]["event"].text == "person detected entering front_porch on front_door"


@respx.mock
def test_frigate_passes_the_snapshot_to_the_orchestrator_when_vision_fetched_it(tmp_path) -> None:
    respx.get("http://127.0.0.1:5000/api/events/evt-1/snapshot.jpg").mock(
        return_value=httpx.Response(200, content=b"fake-jpeg-bytes")
    )
    bridge = _frigate(tmp_path)
    vision = patch("knock.integrations.frigate.observe_scene")
    bridge.vision_provider = object()  # type: ignore[assignment]
    calls = _recording(bridge)

    with vision as observe:
        observe.return_value = None
        bridge.handle_detection(_detection(has_snapshot=True))

    assert calls[0]["kwargs"] == {"image": b"fake-jpeg-bytes"}


def test_home_assistant_asks_the_orchestrator_once_with_no_image(tmp_path) -> None:
    bridge = _ha(tmp_path)
    calls = _recording(bridge)

    bridge.handle_state_changed(_HA_EVENT)

    assert len(calls) == 1
    assert calls[0]["kwargs"] == {}
    assert calls[0]["event"].source == "ha-binary_sensor.front_doorbell"
    assert calls[0]["event"].text == "binary_sensor.front_doorbell triggered"


# -- both greet in their reply text, and both audit ----------------------------------------


def test_the_first_reply_of_a_one_shot_flow_carries_the_greeting(tmp_path) -> None:
    frigate = _frigate(tmp_path / "f")
    ha = _ha(tmp_path / "h")
    greeting = (
        Orchestrator().respond(_recording_event(), state=SessionState(session_id="probe")).text
    )
    suppressed = (
        Orchestrator()
        .respond(
            _recording_event(), state=SessionState(session_id="probe2"), suppress_greeting=True
        )
        .text
    )
    assert greeting != suppressed  # the greeting is part of the text unless suppressed

    assert frigate.handle_detection(_detection()).text == greeting
    ha_decision = ha.handle_state_changed(_HA_EVENT)
    assert ha_decision is not None and ha_decision.text == greeting


def _recording_event():
    from knock.core.events import VisitorEvent

    return VisitorEvent(
        source="probe",
        text="person detected entering front_porch on front_door",
        timestamp=datetime.now(UTC),
    )


def test_each_one_shot_flow_writes_one_audit_entry_under_its_session(tmp_path) -> None:
    frigate = _frigate(tmp_path / "f")
    ha = _ha(tmp_path / "h")

    frigate.handle_detection(_detection())
    ha.handle_state_changed(_HA_EVENT)

    [f_entry] = frigate.audit_log.recent()  # type: ignore[attr-defined]
    [h_entry] = ha.audit_log.recent()  # type: ignore[attr-defined]
    assert f_entry.session_id == "frigate-front_door"
    assert h_entry.session_id == "ha-binary_sensor_front_doorbell"
