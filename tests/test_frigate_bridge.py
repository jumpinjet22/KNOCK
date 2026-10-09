import json
from unittest.mock import MagicMock, patch

import httpx
import respx

import knock.integrations.frigate as frigate_module
from knock.config import FrigateConfig
from knock.core.audit import JSONLAuditLog
from knock.core.scene import SceneObservation
from knock.core.session_store import JSONFileSessionStore
from knock.integrations.frigate import FrigateBridge, FrigateDetection, _default_session_id


def _bridge(tmp_path, **config_overrides) -> FrigateBridge:
    config = FrigateConfig(topic_prefix="frigate", topic_out="knock/responses", **config_overrides)
    return FrigateBridge(config=config, session_store=JSONFileSessionStore(tmp_path))


def _new_event_payload(
    label: str = "person",
    camera: str = "front_door",
    entered_zones: list[str] | None = None,
    has_snapshot: bool = True,
    event_id: str = "evt-1",
    event_type: str = "new",
) -> bytes:
    payload = {
        "type": event_type,
        "before": {},
        "after": {
            "id": event_id,
            "camera": camera,
            "label": label,
            "entered_zones": entered_zones if entered_zones is not None else ["front_porch"],
            "has_snapshot": has_snapshot,
        },
    }
    return json.dumps(payload).encode("utf-8")


def test_default_session_id_sanitizes_unsafe_characters() -> None:
    assert _default_session_id("front/door") == "frigate-front_door"
    assert _default_session_id("") == "frigate-unknown"


def test_parse_event_matches_default_trigger_label_and_zone(tmp_path) -> None:
    bridge = _bridge(tmp_path)
    detection = bridge.parse_event(_new_event_payload())

    assert detection == FrigateDetection(
        event_id="evt-1",
        camera="front_door",
        label="person",
        zones=("front_porch",),
        has_snapshot=True,
    )


def test_parse_event_ignores_non_new_events(tmp_path) -> None:
    bridge = _bridge(tmp_path)
    assert bridge.parse_event(_new_event_payload(event_type="update")) is None
    assert bridge.parse_event(_new_event_payload(event_type="end")) is None


def test_parse_event_ignores_unmatched_label(tmp_path) -> None:
    bridge = _bridge(tmp_path, trigger_labels=["person"])
    assert bridge.parse_event(_new_event_payload(label="car")) is None


def test_parse_event_ignores_detection_with_no_zone_entry(tmp_path) -> None:
    bridge = _bridge(tmp_path)
    assert bridge.parse_event(_new_event_payload(entered_zones=[])) is None


def test_parse_event_honors_configured_zone_filter(tmp_path) -> None:
    bridge = _bridge(tmp_path, zones=["backyard"])
    assert bridge.parse_event(_new_event_payload(entered_zones=["front_porch"])) is None
    assert bridge.parse_event(_new_event_payload(entered_zones=["backyard"])) is not None


def test_handle_detection_persists_session_and_builds_text(tmp_path) -> None:
    bridge = _bridge(tmp_path)
    detection = FrigateDetection(
        event_id="evt-1",
        camera="front_door",
        label="person",
        zones=("front_porch",),
        has_snapshot=False,
    )

    decision = bridge.handle_detection(detection)

    assert decision.text  # a real canned response came back from the orchestrator
    state = bridge.session_store.load("frigate-front_door")
    assert state is not None
    assert state.turn_count == 1


@respx.mock
def test_handle_detection_enriches_with_vision_description(tmp_path) -> None:
    respx.get("http://127.0.0.1:5000/api/events/evt-1/snapshot.jpg").mock(
        return_value=httpx.Response(200, content=b"fake-jpeg-bytes")
    )
    vision_provider = MagicMock()
    vision_provider.describe.return_value = "a person holding a box"
    bridge = _bridge(tmp_path, http_host="127.0.0.1", http_port=5000)
    bridge.vision_provider = vision_provider

    detection = FrigateDetection(
        event_id="evt-1",
        camera="front_door",
        label="person",
        zones=("front_porch",),
        has_snapshot=True,
    )

    bridge.handle_detection(detection)

    vision_provider.describe.assert_called_once_with(b"fake-jpeg-bytes")


@respx.mock
def test_handle_detection_keeps_camera_context_out_of_the_visitor_text(tmp_path) -> None:
    respx.get("http://127.0.0.1:5000/api/events/evt-1/snapshot.jpg").mock(
        return_value=httpx.Response(200, content=b"fake-jpeg-bytes")
    )

    class _ObservingVision:
        name = "observing-vision"

        def describe(self, image: bytes, prompt: str | None = None) -> str:
            raise AssertionError("observe() should be used, not describe()")

        def observe(self, image: bytes) -> SceneObservation:
            return SceneObservation(carrying=["package"], summary="A person holding a box.")

    audit_log = JSONLAuditLog(tmp_path / "audit.jsonl")
    bridge = _bridge(tmp_path, http_host="127.0.0.1", http_port=5000)
    bridge.vision_provider = _ObservingVision()
    bridge.audit_log = audit_log

    bridge.handle_detection(
        FrigateDetection(
            event_id="evt-1",
            camera="front_door",
            label="person",
            zones=("front_porch",),
            has_snapshot=True,
        )
    )

    entry = audit_log.recent()[0]
    assert entry.text == "person detected entering front_porch on front_door"
    assert entry.scene is not None
    assert entry.scene.camera == "front_door"
    assert entry.scene.detected_labels == ["person"]
    assert entry.scene.zones == ["front_porch"]
    assert entry.scene.observation is not None
    assert entry.scene.observation.summary == "A person holding a box."


@respx.mock
def test_handle_detection_survives_vision_failure(tmp_path) -> None:
    respx.get("http://127.0.0.1:5000/api/events/evt-1/snapshot.jpg").mock(
        return_value=httpx.Response(500)
    )
    vision_provider = MagicMock()
    bridge = _bridge(tmp_path, http_host="127.0.0.1", http_port=5000)
    bridge.vision_provider = vision_provider

    detection = FrigateDetection(
        event_id="evt-1",
        camera="front_door",
        label="person",
        zones=("front_porch",),
        has_snapshot=True,
    )

    decision = bridge.handle_detection(detection)

    assert decision.text  # still answered, despite the vision/snapshot failure
    vision_provider.describe.assert_not_called()


def test_on_connect_subscribes_to_events_topic(tmp_path) -> None:
    bridge = _bridge(tmp_path)
    client = MagicMock()

    bridge._on_connect(client, None, {}, 0, None)

    client.subscribe.assert_called_once_with("frigate/events")


def test_on_message_publishes_response_for_matching_detection(tmp_path) -> None:
    bridge = _bridge(tmp_path)
    client = MagicMock()
    msg = MagicMock()
    msg.payload = _new_event_payload()
    msg.topic = "frigate/events"

    bridge._on_message(client, None, msg)

    client.publish.assert_called_once()
    topic, _payload = client.publish.call_args[0]
    assert topic == "knock/responses"


def test_on_message_ignores_non_triggering_detection_without_publishing(tmp_path) -> None:
    bridge = _bridge(tmp_path)
    client = MagicMock()
    msg = MagicMock()
    msg.payload = _new_event_payload(entered_zones=[])
    msg.topic = "frigate/events"

    bridge._on_message(client, None, msg)

    client.publish.assert_not_called()


def test_on_message_ignores_unparseable_payload(tmp_path) -> None:
    bridge = _bridge(tmp_path)
    client = MagicMock()
    msg = MagicMock()
    msg.payload = b"not json"
    msg.topic = "frigate/events"

    bridge._on_message(client, None, msg)

    client.publish.assert_not_called()


def test_frigate_config_defaults() -> None:
    config = FrigateConfig()
    assert config.mqtt_host == "127.0.0.1"
    assert config.topic_prefix == "frigate"
    assert config.trigger_labels == ["person"]
    assert config.zones == []


def test_frigate_config_from_env(monkeypatch) -> None:
    monkeypatch.setenv("KNOCK_FRIGATE_MQTT_HOST", "frigate.local")
    monkeypatch.setenv("KNOCK_FRIGATE_TRIGGER_LABELS", "person, package")
    monkeypatch.setenv("KNOCK_FRIGATE_ZONES", "front_porch")

    config = FrigateConfig.from_env()

    assert config.mqtt_host == "frigate.local"
    assert config.trigger_labels == ["person", "package"]
    assert config.zones == ["front_porch"]


def test_main_wires_a_real_audit_log_not_the_null_default(tmp_path, monkeypatch) -> None:
    # See the matching test in test_unifi_bridge.py for the full incident:
    # main() previously left audit_log on its NullAuditLog class default,
    # so this bridge (like all four) never actually wrote to audit.jsonl
    # in production despite real conversations happening.
    monkeypatch.setenv("KNOCK_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    with patch.object(frigate_module, "FrigateBridge") as bridge_cls:
        frigate_module.main()

    kwargs = bridge_cls.call_args.kwargs
    assert isinstance(kwargs["audit_log"], JSONLAuditLog)
    assert kwargs["audit_log"].path == tmp_path / "audit.jsonl"
