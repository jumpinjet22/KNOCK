"""Home Assistant bridge: feeds entity state changes into the orchestrator.

Authenticates with a Long-Lived Access Token (create one in your HA user
profile), subscribes to `state_changed` events over HA's WebSocket API
(`/api/websocket`) -- which has no server-side entity filter for this event
type, so KNOCK filters client-side to one `trigger_entity_id` -- and treats
any genuine state transition on that entity as a visitor event. Checking
for "any change" rather than specifically "became on" is deliberate: a
`binary_sensor` doorbell goes off->on, but a growing number of HA doorbell
buttons are modeled as an `event` entity whose state is just a timestamp
that changes on every press, not an on/off value. "unknown"/"unavailable"
placeholder states (entity not yet initialized, or the device dropped
offline) are never treated as a trigger.

If `notify_service` is configured, the resulting response text is sent
through that Home Assistant service afterward (REST `POST
/api/services/<domain>/<service>`).

**Safety note:** this bridge only ever calls whatever service *you*
configure via `notify_service` -- it ships with no default that unlocks,
arms, or disarms anything. That's entirely your own Home Assistant
configuration choice.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx
import websockets

from knock.config import HomeAssistantConfig, OllamaConfig
from knock.core.audit import AuditLog, JSONLAuditLog, NullAuditLog
from knock.core.events import VisitorEvent
from knock.core.orchestrator import Orchestrator
from knock.core.responses import ResponseDecision
from knock.core.session_store import JSONFileSessionStore, SessionStore
from knock.core.state import SessionState
from knock.providers.llm.ollama import OllamaProvider

logger = logging.getLogger(__name__)

_UNSAFE_SESSION_CHARS = re.compile(r"[^A-Za-z0-9_-]")
_IGNORED_STATES = {None, "unknown", "unavailable"}

# Notification-action identifiers KNOCK sends (see UnifiBridge's
# signature-required notification) and the listener below reacts to. The
# device id rides along embedded in the action string itself (see
# ACTION_DEVICE_ID_SEP) rather than depending on whichever extra context a
# given Home Assistant mobile app version does or doesn't echo back on the
# action event -- the `action` identifier is the one thing guaranteed to
# round-trip verbatim.
KNOCK_ON_MY_WAY_ACTION = "knock_on_my_way"
KNOCK_TURN_AWAY_ACTION = "knock_turn_away"
# A single-button acknowledgement (not an accept/reject pair like the two
# above) -- used for a service_appointment notification, where there's no
# "turn them away" decision to make (the technician is already expected),
# just a way to tell a waiting visitor someone's coming without needing
# the app's live-talk feature for something this simple.
KNOCK_COMING_TO_DOOR_ACTION = "knock_coming_to_door"
ACTION_DEVICE_ID_SEP = "::"


def _default_session_id(entity_id: str) -> str:
    safe = _UNSAFE_SESSION_CHARS.sub("_", entity_id) or "unknown"
    return f"ha-{safe}"[:128]


class HomeAssistantNotifier:
    """Thin REST client for calling one Home Assistant service.

    Factored out of `HomeAssistantBridge` so any other bridge can notify the
    household too (e.g. UnifiBridge, on a signature-required delivery)
    without each needing its own HA REST/auth plumbing.
    """

    def __init__(
        self, config: HomeAssistantConfig, http_client: httpx.Client | None = None
    ) -> None:
        self.config = config
        self._http_client = http_client or httpx.Client(
            timeout=10.0,
            verify=config.verify_ssl,
            headers={"Authorization": f"Bearer {config.token.get_secret_value()}"},
        )

    def notify(self, message: str, *, actions: list[dict[str, str]] | None = None) -> None:
        """No-op if `notify_service` isn't configured -- callers don't need
        to check that themselves before calling this.

        `actions` adds interactive buttons to the notification (the Home
        Assistant mobile app's own notification-actions feature -- each is
        `{"action": "<identifier>", "title": "<button label>"}`). KNOCK only
        *sends* them here; reacting to which one was tapped is a Home
        Assistant automation on the `mobile_app_notification_action` event,
        not something this call waits for or knows about.
        """
        if not self.config.notify_service:
            return
        domain, _, name = self.config.notify_service.partition(".")
        payload: dict[str, Any] = {"message": message}
        if actions:
            payload["data"] = {"actions": actions}
        response = self._http_client.post(
            f"{self.config.base_url}/api/services/{domain}/{name}",
            json=payload,
        )
        response.raise_for_status()


def websocket_url(base_url: str) -> str:
    """Convert an HA base URL (http(s)://...) into its websocket equivalent."""
    if base_url.startswith("https://"):
        return "wss://" + base_url[len("https://") :].rstrip("/") + "/api/websocket"
    if base_url.startswith("http://"):
        return "ws://" + base_url[len("http://") :].rstrip("/") + "/api/websocket"
    raise ValueError(f"base_url must start with http:// or https://: {base_url!r}")


class _WebSocketLike(Protocol):
    async def send(self, message: str) -> None: ...
    async def recv(self) -> str | bytes: ...
    def __aiter__(self) -> AsyncIterator[str | bytes]: ...


class HomeAssistantBridge:
    """Subscribes to one entity's `state_changed` events, answers each one."""

    def __init__(
        self,
        config: HomeAssistantConfig | None = None,
        orchestrator: Orchestrator | None = None,
        session_store: SessionStore | None = None,
        audit_log: AuditLog | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.config = config or HomeAssistantConfig()
        self.orchestrator = orchestrator or Orchestrator()
        self.session_store = session_store or JSONFileSessionStore()
        self.audit_log = audit_log or NullAuditLog()
        self._http_client = http_client or httpx.Client(
            timeout=10.0,
            verify=self.config.verify_ssl,
            headers={"Authorization": f"Bearer {self.config.token.get_secret_value()}"},
        )
        self._notifier = HomeAssistantNotifier(self.config, http_client=self._http_client)
        self._request_ids = itertools.count(1)

    # -- pure logic: directly testable without a real websocket -----------------

    def should_trigger(self, event_data: dict[str, Any]) -> bool:
        if event_data.get("entity_id") != self.config.trigger_entity_id:
            return False

        new_state = event_data.get("new_state") or {}
        new_value = new_state.get("state")
        if new_value in _IGNORED_STATES:
            return False

        old_state = event_data.get("old_state") or {}
        return new_value != old_state.get("state")

    def build_event(self, event_data: dict[str, Any]) -> VisitorEvent:
        entity_id = event_data["entity_id"]
        return VisitorEvent(
            source=f"ha-{entity_id}",
            text=f"{entity_id} triggered",
            timestamp=datetime.now(UTC),
        )

    def handle_state_changed(self, event_data: dict[str, Any]) -> ResponseDecision | None:
        if not self.should_trigger(event_data):
            return None

        event = self.build_event(event_data)
        session_id = _default_session_id(event_data["entity_id"])
        state = self.session_store.load(session_id) or SessionState(
            session_id=session_id, updated_at=event.timestamp
        )
        decision = self.orchestrator.respond(event, state=state, audit_log=self.audit_log)
        self.session_store.save(state)

        try:
            self._notifier.notify(decision.text)
        except Exception as exc:  # noqa: BLE001 - the notify call is best-effort
            logger.warning("Home Assistant notify service call failed: %s", exc)

        return decision

    async def handle_message(self, raw_message: str | bytes) -> ResponseDecision | None:
        message = json.loads(raw_message)
        if message.get("type") != "event":
            return None
        event = message.get("event") or {}
        if event.get("event_type") != "state_changed":
            return None
        return self.handle_state_changed(event.get("data") or {})

    # -- websocket lifecycle -------------------------------------------------

    async def run(self) -> None:
        async with websockets.connect(websocket_url(self.config.base_url)) as ws:
            await self._authenticate(ws)
            await self._subscribe(ws)
            async for raw_message in ws:
                await self.handle_message(raw_message)

    async def _authenticate(self, ws: _WebSocketLike) -> None:
        hello = json.loads(await ws.recv())
        if hello.get("type") != "auth_required":
            raise ConnectionError(f"Unexpected Home Assistant handshake message: {hello}")

        await ws.send(
            json.dumps({"type": "auth", "access_token": self.config.token.get_secret_value()})
        )
        response = json.loads(await ws.recv())
        if response.get("type") != "auth_ok":
            raise PermissionError(f"Home Assistant authentication failed: {response}")

    async def _subscribe(self, ws: _WebSocketLike) -> None:
        await ws.send(
            json.dumps(
                {
                    "id": next(self._request_ids),
                    "type": "subscribe_events",
                    "event_type": "state_changed",
                }
            )
        )
        ack = json.loads(await ws.recv())
        if not ack.get("success", False):
            raise ConnectionError(f"Failed to subscribe to state_changed events: {ack}")


def parse_action_device_id(raw_action: str) -> tuple[str, str] | None:
    """Split `f"{action_id}{ACTION_DEVICE_ID_SEP}{device_id}"` back apart,
    or `None` if `raw_action` isn't one of KNOCK's own action identifiers.
    """
    action_id, sep, device_id = raw_action.partition(ACTION_DEVICE_ID_SEP)
    if not sep or not device_id:
        return None
    return action_id, device_id


class HomeAssistantActionListener:
    """Reacts to a Home Assistant mobile app notification-action tap (e.g.
    the "I'm on my way" / "Turn them away" buttons on a signature-required
    delivery notification -- see `UnifiBridge`).

    Runs its own persistent websocket connection, independent of (and
    typically alongside -- see `unifi.main()`) a `HomeAssistantBridge`'s
    own connection, since the two subscribe to different event types for
    different reasons.
    """

    def __init__(
        self,
        config: HomeAssistantConfig | None = None,
        on_action: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> None:
        self.config = config or HomeAssistantConfig()
        self._on_action = on_action
        self._request_ids = itertools.count(1)

    async def handle_message(self, raw_message: str | bytes) -> None:
        message = json.loads(raw_message)
        if message.get("type") != "event":
            return
        event = message.get("event") or {}
        if event.get("event_type") != "mobile_app_notification_action":
            return

        raw_action = (event.get("data") or {}).get("action")
        if not raw_action:
            return
        parsed = parse_action_device_id(raw_action)
        if parsed is None:
            return

        if self._on_action is not None:
            await self._on_action(*parsed)

    async def run(self) -> None:
        async with websockets.connect(websocket_url(self.config.base_url)) as ws:
            await self._authenticate(ws)
            await self._subscribe(ws)
            async for raw_message in ws:
                try:
                    await self.handle_message(raw_message)
                except Exception as exc:  # noqa: BLE001 - keep listening either way
                    logger.warning("Failed to handle a notification action event: %s", exc)

    async def _authenticate(self, ws: _WebSocketLike) -> None:
        hello = json.loads(await ws.recv())
        if hello.get("type") != "auth_required":
            raise ConnectionError(f"Unexpected Home Assistant handshake message: {hello}")

        await ws.send(
            json.dumps({"type": "auth", "access_token": self.config.token.get_secret_value()})
        )
        response = json.loads(await ws.recv())
        if response.get("type") != "auth_ok":
            raise PermissionError(f"Home Assistant authentication failed: {response}")

    async def _subscribe(self, ws: _WebSocketLike) -> None:
        await ws.send(
            json.dumps(
                {
                    "id": next(self._request_ids),
                    "type": "subscribe_events",
                    "event_type": "mobile_app_notification_action",
                }
            )
        )
        ack = json.loads(await ws.recv())
        if not ack.get("success", False):
            raise ConnectionError(f"Failed to subscribe to notification action events: {ack}")


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    orchestrator = Orchestrator(llm_provider=OllamaProvider(config=OllamaConfig.from_env()))
    bridge = HomeAssistantBridge(
        config=HomeAssistantConfig.from_env(),
        orchestrator=orchestrator,
        # Without this, the bridge falls back to its class default
        # (NullAuditLog) and every real conversation silently never reaches
        # audit.jsonl / the web UI's History page -- the CLI and web API
        # already construct a real JSONLAuditLog() the same way.
        audit_log=JSONLAuditLog(),
    )
    logger.info(
        "Starting KNOCK Home Assistant bridge: %s (entity=%s)",
        bridge.config.base_url,
        bridge.config.trigger_entity_id,
    )
    asyncio.run(bridge.run())


if __name__ == "__main__":
    main()
