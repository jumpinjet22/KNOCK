import json
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from knock.config import MqttConfig
from knock.core.session_store import JSONFileSessionStore
from knock.integrations.mqtt import MqttBridge, _default_session_id


def _bridge(tmp_path) -> MqttBridge:
    return MqttBridge(
        config=MqttConfig(topic_in="knock/events", topic_out="knock/responses"),
        session_store=JSONFileSessionStore(tmp_path),
    )


def _message(payload: dict) -> MagicMock:
    msg = MagicMock()
    msg.payload = json.dumps(payload).encode("utf-8")
    msg.topic = "knock/events"
    return msg


def test_default_session_id_sanitizes_unsafe_characters() -> None:
    assert _default_session_id("doorbell/front") == "mqtt-doorbell_front"
    assert _default_session_id("") == "mqtt-unknown"


def test_parse_message_fills_in_defaults() -> None:
    event, session_id = MqttBridge.parse_message(json.dumps({"text": "Hi I have a package"}))
    assert event.text == "Hi I have a package"
    assert event.source == "mqtt"
    assert session_id == "mqtt-mqtt"


def test_parse_message_honors_explicit_fields() -> None:
    event, session_id = MqttBridge.parse_message(
        json.dumps(
            {
                "text": "Hi",
                "source": "front-doorbell",
                "timestamp": "2026-01-01T12:00:00+00:00",
                "session_id": "visit-42",
            }
        )
    )
    assert event.source == "front-doorbell"
    assert event.timestamp == datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    assert session_id == "visit-42"


def test_parse_message_requires_text() -> None:
    with pytest.raises(KeyError):
        MqttBridge.parse_message(json.dumps({"source": "mqtt"}))


def test_handle_event_persists_session_across_calls(tmp_path) -> None:
    bridge = _bridge(tmp_path)
    event, session_id = MqttBridge.parse_message(
        json.dumps({"text": "Hi I have a package", "session_id": "visit-1"})
    )

    first = bridge.handle_event(event, session_id)
    second = bridge.handle_event(event, session_id)

    assert "leave the package" in first.text.lower()
    assert "leave the package" in second.text.lower()
    state = bridge.session_store.load("visit-1")
    assert state is not None
    assert state.turn_count == 2
    # Only the session's first response introduces KNOCK; repeating it every
    # turn would be annoying, so later turns should be the plain canned text.
    assert first.text != second.text
    assert "my name is knock" in first.text.lower()
    assert "my name is knock" not in second.text.lower()


def test_on_connect_subscribes_to_configured_topic(tmp_path) -> None:
    bridge = _bridge(tmp_path)
    client = MagicMock()

    bridge._on_connect(client, None, {}, 0, None)

    client.subscribe.assert_called_once_with("knock/events")


def test_on_message_publishes_response(tmp_path) -> None:
    bridge = _bridge(tmp_path)
    client = MagicMock()
    msg = _message({"text": "Hi I have a package", "session_id": "visit-2"})

    bridge._on_message(client, None, msg)

    client.publish.assert_called_once()
    topic, payload = client.publish.call_args[0]
    assert topic == "knock/responses"
    body = json.loads(payload)
    assert "leave the package" in body["text"].lower()


def test_on_message_ignores_unparseable_payload_without_raising(tmp_path, caplog) -> None:
    bridge = _bridge(tmp_path)
    client = MagicMock()
    msg = MagicMock()
    msg.payload = b"not json"
    msg.topic = "knock/events"

    bridge._on_message(client, None, msg)

    client.publish.assert_not_called()


def test_mqtt_config_defaults() -> None:
    config = MqttConfig()
    assert config.host == "127.0.0.1"
    assert config.port == 1883
    assert config.topic_in == "knock/events"
    assert config.topic_out == "knock/responses"


def test_mqtt_config_from_env(monkeypatch) -> None:
    monkeypatch.setenv("KNOCK_MQTT_HOST", "broker.local")
    monkeypatch.setenv("KNOCK_MQTT_PORT", "8883")
    monkeypatch.setenv("KNOCK_MQTT_TOPIC_IN", "home/doorbell/events")

    config = MqttConfig.from_env()

    assert config.host == "broker.local"
    assert config.port == 8883
    assert config.topic_in == "home/doorbell/events"
