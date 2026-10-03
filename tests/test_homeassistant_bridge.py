import asyncio
import json
from unittest.mock import patch

import httpx
import pytest
import respx

import knock.integrations.homeassistant as homeassistant_module
from knock.config import HomeAssistantConfig
from knock.core.audit import JSONLAuditLog
from knock.core.session_store import JSONFileSessionStore
from knock.integrations.homeassistant import (
    ACTION_DEVICE_ID_SEP,
    HomeAssistantActionListener,
    HomeAssistantBridge,
    HomeAssistantNotifier,
    parse_action_device_id,
    websocket_url,
)


class _FakeWebSocket:
    """Minimal async stand-in for a `websockets` connection."""

    def __init__(self, recv_messages: list[str]) -> None:
        self._recv_messages = iter(recv_messages)
        self.sent: list[str] = []

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def recv(self) -> str:
        return next(self._recv_messages)

    def __aiter__(self) -> "_FakeWebSocket":
        return self

    async def __anext__(self) -> str:
        try:
            return next(self._recv_messages)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


def _bridge(tmp_path, **config_overrides) -> HomeAssistantBridge:
    defaults = {
        "base_url": "http://ha.local:8123",
        "token": "secret-token",
        "trigger_entity_id": "binary_sensor.front_doorbell",
    }
    config = HomeAssistantConfig(**{**defaults, **config_overrides})
    return HomeAssistantBridge(config=config, session_store=JSONFileSessionStore(tmp_path))


def _state_changed(entity_id: str, old: str | None, new: str | None) -> dict:
    return {
        "entity_id": entity_id,
        "old_state": {"state": old} if old is not None else None,
        "new_state": {"state": new} if new is not None else None,
    }


# -- websocket_url ------------------------------------------------------------


def test_websocket_url_converts_http_and_https() -> None:
    assert websocket_url("http://ha.local:8123") == "ws://ha.local:8123/api/websocket"
    assert websocket_url("https://ha.local:8123") == "wss://ha.local:8123/api/websocket"
    assert websocket_url("https://ha.local:8123/") == "wss://ha.local:8123/api/websocket"


def test_websocket_url_rejects_unknown_scheme() -> None:
    with pytest.raises(ValueError):
        websocket_url("ftp://ha.local")


# -- should_trigger ------------------------------------------------------------


def test_should_trigger_on_off_to_on_transition(tmp_path) -> None:
    bridge = _bridge(tmp_path)
    data = _state_changed("binary_sensor.front_doorbell", "off", "on")
    assert bridge.should_trigger(data) is True


def test_should_trigger_on_event_entity_timestamp_change(tmp_path) -> None:
    bridge = _bridge(tmp_path, trigger_entity_id="event.front_doorbell_button")
    data = _state_changed(
        "event.front_doorbell_button", "2026-01-01T00:00:00+00:00", "2026-01-01T00:05:00+00:00"
    )
    assert bridge.should_trigger(data) is True


def test_should_trigger_ignores_unmatched_entity(tmp_path) -> None:
    bridge = _bridge(tmp_path)
    data = _state_changed("binary_sensor.other", "off", "on")
    assert bridge.should_trigger(data) is False


@pytest.mark.parametrize("placeholder", ["unknown", "unavailable", None])
def test_should_trigger_ignores_placeholder_states(tmp_path, placeholder) -> None:
    bridge = _bridge(tmp_path)
    data = _state_changed("binary_sensor.front_doorbell", "off", placeholder)
    assert bridge.should_trigger(data) is False


def test_should_trigger_ignores_attribute_only_change(tmp_path) -> None:
    bridge = _bridge(tmp_path)
    data = _state_changed("binary_sensor.front_doorbell", "on", "on")
    assert bridge.should_trigger(data) is False


# -- handle_state_changed -------------------------------------------------------


def test_handle_state_changed_persists_session(tmp_path) -> None:
    bridge = _bridge(tmp_path)
    data = _state_changed("binary_sensor.front_doorbell", "off", "on")

    decision = bridge.handle_state_changed(data)

    assert decision is not None
    state = bridge.session_store.load("ha-binary_sensor_front_doorbell")
    assert state is not None
    assert state.turn_count == 1


def test_handle_state_changed_returns_none_when_not_triggering(tmp_path) -> None:
    bridge = _bridge(tmp_path)
    data = _state_changed("binary_sensor.other", "off", "on")
    assert bridge.handle_state_changed(data) is None


@respx.mock
def test_handle_state_changed_calls_notify_service_with_bearer_token(tmp_path) -> None:
    route = respx.post("http://ha.local:8123/api/services/notify/mobile_app_test").mock(
        return_value=httpx.Response(200, json={})
    )
    bridge = _bridge(tmp_path, notify_service="notify.mobile_app_test")
    data = _state_changed("binary_sensor.front_doorbell", "off", "on")

    decision = bridge.handle_state_changed(data)

    assert decision is not None
    assert route.called
    assert route.calls.last.request.headers["authorization"] == "Bearer secret-token"
    assert json.loads(route.calls.last.request.content) == {"message": decision.text}


@respx.mock
def test_handle_state_changed_survives_notify_failure(tmp_path) -> None:
    respx.post("http://ha.local:8123/api/services/notify/mobile_app_test").mock(
        return_value=httpx.Response(500)
    )
    bridge = _bridge(tmp_path, notify_service="notify.mobile_app_test")
    data = _state_changed("binary_sensor.front_doorbell", "off", "on")

    decision = bridge.handle_state_changed(data)

    assert decision is not None


# -- handle_message (async dispatch) ---------------------------------------------


def test_handle_message_dispatches_state_changed_event(tmp_path) -> None:
    bridge = _bridge(tmp_path)
    raw = json.dumps(
        {
            "type": "event",
            "event": {
                "event_type": "state_changed",
                "data": _state_changed("binary_sensor.front_doorbell", "off", "on"),
            },
        }
    )

    decision = asyncio.run(bridge.handle_message(raw))

    assert decision is not None
    assert decision.text


def test_handle_message_ignores_non_event_messages(tmp_path) -> None:
    bridge = _bridge(tmp_path)
    raw = json.dumps({"type": "result", "success": True})
    assert asyncio.run(bridge.handle_message(raw)) is None


def test_handle_message_ignores_other_event_types(tmp_path) -> None:
    bridge = _bridge(tmp_path)
    raw = json.dumps({"type": "event", "event": {"event_type": "something_else", "data": {}}})
    assert asyncio.run(bridge.handle_message(raw)) is None


# -- auth / subscribe handshake (fake websocket) ---------------------------------


def test_authenticate_sends_token_after_auth_required(tmp_path) -> None:
    bridge = _bridge(tmp_path, token="secret-token")
    ws = _FakeWebSocket([json.dumps({"type": "auth_required"}), json.dumps({"type": "auth_ok"})])

    asyncio.run(bridge._authenticate(ws))

    assert json.loads(ws.sent[0]) == {"type": "auth", "access_token": "secret-token"}


def test_authenticate_raises_on_invalid_token(tmp_path) -> None:
    bridge = _bridge(tmp_path)
    ws = _FakeWebSocket(
        [json.dumps({"type": "auth_required"}), json.dumps({"type": "auth_invalid"})]
    )

    with pytest.raises(PermissionError):
        asyncio.run(bridge._authenticate(ws))


def test_authenticate_raises_on_unexpected_handshake(tmp_path) -> None:
    bridge = _bridge(tmp_path)
    ws = _FakeWebSocket([json.dumps({"type": "something_else"})])

    with pytest.raises(ConnectionError):
        asyncio.run(bridge._authenticate(ws))


def test_subscribe_sends_state_changed_subscription(tmp_path) -> None:
    bridge = _bridge(tmp_path)
    ws = _FakeWebSocket([json.dumps({"id": 1, "type": "result", "success": True})])

    asyncio.run(bridge._subscribe(ws))

    sent = json.loads(ws.sent[0])
    assert sent["type"] == "subscribe_events"
    assert sent["event_type"] == "state_changed"


def test_subscribe_raises_if_not_successful(tmp_path) -> None:
    bridge = _bridge(tmp_path)
    ws = _FakeWebSocket([json.dumps({"id": 1, "type": "result", "success": False})])

    with pytest.raises(ConnectionError):
        asyncio.run(bridge._subscribe(ws))


# -- config ---------------------------------------------------------------------


def test_home_assistant_config_defaults() -> None:
    config = HomeAssistantConfig()
    assert config.base_url == "http://homeassistant.local:8123"
    assert config.trigger_entity_id == "binary_sensor.front_doorbell"
    assert config.notify_service is None
    assert config.verify_ssl is True


def test_home_assistant_config_from_env(monkeypatch) -> None:
    monkeypatch.setenv("KNOCK_HA_BASE_URL", "http://ha.local:8123")
    monkeypatch.setenv("KNOCK_HA_TOKEN", "abc123")
    monkeypatch.setenv("KNOCK_HA_TRIGGER_ENTITY_ID", "event.doorbell")
    monkeypatch.setenv("KNOCK_HA_NOTIFY_SERVICE", "notify.mobile_app_test")
    monkeypatch.setenv("KNOCK_HA_VERIFY_SSL", "false")

    config = HomeAssistantConfig.from_env()

    assert config.base_url == "http://ha.local:8123"
    assert config.token.get_secret_value() == "abc123"
    assert config.trigger_entity_id == "event.doorbell"
    assert config.notify_service == "notify.mobile_app_test"
    assert config.verify_ssl is False


# -- HomeAssistantNotifier (shared by any bridge, not just this one) ------------------


def _notifier_config(**overrides) -> HomeAssistantConfig:
    defaults = {
        "base_url": "http://ha.local:8123",
        "token": "secret-token",
        "notify_service": "notify.mobile_app_test",
    }
    return HomeAssistantConfig(**{**defaults, **overrides})


@respx.mock
def test_notifier_calls_the_configured_service_with_a_bearer_token() -> None:
    route = respx.post("http://ha.local:8123/api/services/notify/mobile_app_test").mock(
        return_value=httpx.Response(200, json={})
    )
    notifier = HomeAssistantNotifier(_notifier_config())

    notifier.notify("A delivery needs a signature.")

    assert route.called
    assert route.calls.last.request.headers["authorization"] == "Bearer secret-token"
    assert json.loads(route.calls.last.request.content) == {
        "message": "A delivery needs a signature."
    }


@respx.mock
def test_notifier_includes_action_buttons_when_given() -> None:
    route = respx.post("http://ha.local:8123/api/services/notify/mobile_app_test").mock(
        return_value=httpx.Response(200, json={})
    )
    notifier = HomeAssistantNotifier(_notifier_config())

    notifier.notify(
        "A delivery needs a signature.",
        actions=[
            {"action": "knock_on_my_way", "title": "I'm on my way"},
            {"action": "knock_turn_away", "title": "Turn them away"},
        ],
    )

    body = json.loads(route.calls.last.request.content)
    assert body["data"]["actions"] == [
        {"action": "knock_on_my_way", "title": "I'm on my way"},
        {"action": "knock_turn_away", "title": "Turn them away"},
    ]


def test_notifier_is_a_no_op_without_a_configured_service() -> None:
    notifier = HomeAssistantNotifier(_notifier_config(notify_service=None))
    notifier.notify("should not be sent anywhere")  # should not raise


# -- parse_action_device_id -------------------------------------------------------


def test_parse_action_device_id_splits_on_separator() -> None:
    raw = f"knock_on_my_way{ACTION_DEVICE_ID_SEP}cam1"
    assert parse_action_device_id(raw) == ("knock_on_my_way", "cam1")


def test_parse_action_device_id_returns_none_without_separator() -> None:
    assert parse_action_device_id("some_unrelated_action") is None


def test_parse_action_device_id_returns_none_with_empty_device_id() -> None:
    assert parse_action_device_id(f"knock_on_my_way{ACTION_DEVICE_ID_SEP}") is None


# -- HomeAssistantActionListener ---------------------------------------------------


def _listener_config(**overrides) -> HomeAssistantConfig:
    defaults = {"base_url": "http://ha.local:8123", "token": "secret-token"}
    return HomeAssistantConfig(**{**defaults, **overrides})


def test_listener_subscribes_to_notification_action_events() -> None:
    listener = HomeAssistantActionListener(config=_listener_config())
    ws = _FakeWebSocket([json.dumps({"id": 1, "type": "result", "success": True})])

    asyncio.run(listener._subscribe(ws))

    sent = json.loads(ws.sent[0])
    assert sent["type"] == "subscribe_events"
    assert sent["event_type"] == "mobile_app_notification_action"


def test_listener_subscribe_raises_if_not_successful() -> None:
    listener = HomeAssistantActionListener(config=_listener_config())
    ws = _FakeWebSocket([json.dumps({"id": 1, "type": "result", "success": False})])

    with pytest.raises(ConnectionError):
        asyncio.run(listener._subscribe(ws))


def test_listener_authenticate_sends_token() -> None:
    listener = HomeAssistantActionListener(config=_listener_config(token="secret-token"))
    ws = _FakeWebSocket([json.dumps({"type": "auth_required"}), json.dumps({"type": "auth_ok"})])

    asyncio.run(listener._authenticate(ws))

    assert json.loads(ws.sent[0]) == {"type": "auth", "access_token": "secret-token"}


def test_listener_authenticate_raises_on_invalid_token() -> None:
    listener = HomeAssistantActionListener(config=_listener_config())
    ws = _FakeWebSocket(
        [json.dumps({"type": "auth_required"}), json.dumps({"type": "auth_invalid"})]
    )

    with pytest.raises(PermissionError):
        asyncio.run(listener._authenticate(ws))


def test_listener_handle_message_calls_on_action_for_a_recognized_action_event() -> None:
    calls: list[tuple[str, str]] = []

    async def on_action(action_id: str, device_id: str) -> None:
        calls.append((action_id, device_id))

    listener = HomeAssistantActionListener(config=_listener_config(), on_action=on_action)
    raw = json.dumps(
        {
            "type": "event",
            "event": {
                "event_type": "mobile_app_notification_action",
                "data": {"action": f"knock_on_my_way{ACTION_DEVICE_ID_SEP}cam1"},
            },
        }
    )

    asyncio.run(listener.handle_message(raw))

    assert calls == [("knock_on_my_way", "cam1")]


def test_listener_handle_message_ignores_other_event_types() -> None:
    calls: list[tuple[str, str]] = []

    async def on_action(action_id: str, device_id: str) -> None:
        calls.append((action_id, device_id))

    listener = HomeAssistantActionListener(config=_listener_config(), on_action=on_action)
    raw = json.dumps({"type": "event", "event": {"event_type": "something_else", "data": {}}})

    asyncio.run(listener.handle_message(raw))

    assert calls == []


def test_listener_handle_message_ignores_an_unparseable_action() -> None:
    calls: list[tuple[str, str]] = []

    async def on_action(action_id: str, device_id: str) -> None:
        calls.append((action_id, device_id))

    listener = HomeAssistantActionListener(config=_listener_config(), on_action=on_action)
    raw = json.dumps(
        {
            "type": "event",
            "event": {
                "event_type": "mobile_app_notification_action",
                "data": {"action": "no_separator_here"},
            },
        }
    )

    asyncio.run(listener.handle_message(raw))

    assert calls == []


def test_listener_handle_message_is_a_no_op_without_an_on_action_callback() -> None:
    listener = HomeAssistantActionListener(config=_listener_config())
    raw = json.dumps(
        {
            "type": "event",
            "event": {
                "event_type": "mobile_app_notification_action",
                "data": {"action": f"knock_on_my_way{ACTION_DEVICE_ID_SEP}cam1"},
            },
        }
    )

    asyncio.run(listener.handle_message(raw))  # should not raise


@respx.mock
def test_notifier_raises_on_an_http_error_let_the_caller_decide_how_to_handle_it() -> None:
    respx.post("http://ha.local:8123/api/services/notify/mobile_app_test").mock(
        return_value=httpx.Response(500)
    )
    notifier = HomeAssistantNotifier(_notifier_config())

    with pytest.raises(httpx.HTTPStatusError):
        notifier.notify("hello")


def test_main_wires_a_real_audit_log_not_the_null_default(tmp_path, monkeypatch) -> None:
    # See the matching test in test_unifi_bridge.py for the full incident:
    # main() previously left audit_log on its NullAuditLog class default,
    # so this bridge (like all four) never actually wrote to audit.jsonl
    # in production despite real conversations happening.
    monkeypatch.setenv("KNOCK_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    with (
        patch.object(homeassistant_module, "HomeAssistantBridge") as bridge_cls,
        patch.object(homeassistant_module.asyncio, "run"),
    ):
        homeassistant_module.main()

    kwargs = bridge_cls.call_args.kwargs
    assert isinstance(kwargs["audit_log"], JSONLAuditLog)
    assert kwargs["audit_log"].path == tmp_path / "audit.jsonl"
