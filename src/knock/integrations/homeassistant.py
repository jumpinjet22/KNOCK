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
from typing import Any, Literal, Protocol

import httpx
import websockets

from knock.config import HomeAssistantConfig, OllamaConfig
from knock.core.audit import AuditLog, JSONLAuditLog, NullAuditLog
from knock.core.events import VisitorEvent
from knock.core.orchestrator import Orchestrator
from knock.core.responses import ResponseDecision
from knock.core.session_store import JSONFileSessionStore, SessionStore
from knock.core.state import SessionState
from knock.core.tool_calling_detection import resolve_tool_calling
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


# Lets the same three kinds of door event ring differently on a phone,
# instead of every notification looking and sounding identical regardless
# of urgency. "channel" is an Android notification channel name -- the
# companion app creates it the first time it's used, but Android itself
# (not Home Assistant, not KNOCK) only lets the *user* assign that
# channel's sound/importance, one time, via Settings > Apps > Home
# Assistant > Notifications > [channel name]. "push" configures the iOS
# equivalent: "interruption-level" controls whether it bypasses Focus/
# silent mode ("critical" -- requires the household to have granted the
# Home Assistant app's one-time "Critical Notifications" permission, or
# iOS silently treats it as a normal alert instead) all the way down to
# "passive" (no sound/vibration, appears in the notification list only).
NotificationCategory = Literal["emergency", "approval", "fyi", "review"]

_CATEGORY_CHANNELS: dict[NotificationCategory, str] = {
    "emergency": "knock_emergency",
    "approval": "knock_approval",
    "fyi": "knock_fyi",
    "review": "knock_review",
}

_CATEGORY_PUSH: dict[NotificationCategory, dict[str, Any]] = {
    "emergency": {
        "interruption-level": "critical",
        "sound": {"name": "default", "critical": 1, "volume": 1.0},
    },
    "approval": {"interruption-level": "time-sensitive"},
    "fyi": {"interruption-level": "passive"},
    # Same urgency as "approval" (a human needs to look now, the visitor is
    # still at the door) but its own channel/category -- "approval"'s
    # existing meaning throughout this codebase is "there are yes/no action
    # buttons," which a low-confidence classification notification doesn't
    # necessarily have, and "fyi" is deliberately passive/silent, which
    # would defeat the point of surfacing an otherwise-silent "unknown".
    "review": {"interruption-level": "time-sensitive"},
}


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

    def notify(
        self,
        message: str,
        *,
        actions: list[dict[str, str]] | None = None,
        category: NotificationCategory = "fyi",
        image: str | None = None,
    ) -> None:
        """No-op if `notify_service` isn't configured -- callers don't need
        to check that themselves before calling this.

        `actions` adds interactive buttons to the notification (the Home
        Assistant mobile app's own notification-actions feature -- each is
        `{"action": "<identifier>", "title": "<button label>"}`). KNOCK only
        *sends* them here; reacting to which one was tapped is a Home
        Assistant automation on the `mobile_app_notification_action` event,
        not something this call waits for or knows about.

        `category` picks the Android channel / iOS interruption-level (see
        `_CATEGORY_CHANNELS`/`_CATEGORY_PUSH` above) so emergency, approval-
        needed, and plain-FYI door events can be told apart by sound/
        priority on a phone, not just by the message text. Defaults to the
        least intrusive tier ("fyi") if a caller doesn't specify one.

        `image` is a URL the Home Assistant mobile app fetches directly from
        the phone (see core/snapshot_store.py/api/snapshot_routes.py for how
        callers get one) -- not authenticated, since the phone can't present
        KNOCK's session cookie.
        """
        if not self.config.notify_service:
            return
        domain, _, name = self.config.notify_service.partition(".")
        data: dict[str, Any] = {
            "channel": _CATEGORY_CHANNELS[category],
            "tag": f"knock-{category}",
            "push": _CATEGORY_PUSH[category],
        }
        if actions:
            data["actions"] = actions
        if image:
            data["image"] = image
        payload: dict[str, Any] = {"message": message, "data": data}
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
            if decision.escalate:
                category: NotificationCategory = "emergency"
            elif decision.needs_review:
                category = "review"
            else:
                category = "fyi"
            message = decision.text
            if decision.needs_review and decision.review_candidates:
                message = f"{message} (best guesses: {', '.join(decision.review_candidates)})"
            self._notifier.notify(message, category=category)
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
    ollama_config = OllamaConfig.from_env()
    # None (unset) leaves this whole layer off -- the deterministic
    # apply_style() backstop still applies regardless. When set, this is a
    # second OllamaProvider pointed at a different model (same host/port/
    # timeout) so a bigger/more careful model can be used for this one
    # extra-opinion call without slowing down every-turn response phrasing.
    safety_check_provider = (
        OllamaProvider(
            config=ollama_config.model_copy(update={"model": ollama_config.safety_check_model})
        )
        if ollama_config.safety_check_model
        else None
    )
    orchestrator = Orchestrator(
        llm_provider=OllamaProvider(config=ollama_config),
        use_tool_calling=resolve_tool_calling(ollama_config),
        safety_check_provider=safety_check_provider,
    )
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
