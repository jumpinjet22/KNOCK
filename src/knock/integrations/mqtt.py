"""MQTT bridge: feeds visitor events from a broker into the orchestrator.

Compatible with any MQTT broker (Mosquitto, Home Assistant's built-in
broker, etc). A doorbell/camera integration that already speaks MQTT --
Frigate, an ESPHome button, a Home Assistant automation -- can feed KNOCK by
publishing a JSON payload to `config.topic_in`:

    {"source": "doorbell", "text": "...", "timestamp": "...", "session_id": "..."}

Only `text` is required; `source` defaults to "mqtt", `timestamp` defaults
to now, and `session_id` (if omitted) is derived from `source` so repeated
messages from the same device keep one running conversation. The resulting
`ResponseDecision` is published as JSON to `config.topic_out`.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime

import paho.mqtt.client as mqtt

from knock.config import MqttConfig, OllamaConfig
from knock.core.audit import AuditLog, NullAuditLog
from knock.core.events import VisitorEvent
from knock.core.orchestrator import Orchestrator
from knock.core.responses import ResponseDecision
from knock.core.session_store import JSONFileSessionStore, SessionStore
from knock.core.state import SessionState
from knock.providers.llm.ollama import OllamaProvider

logger = logging.getLogger(__name__)

_UNSAFE_SESSION_CHARS = re.compile(r"[^A-Za-z0-9_-]")


def _default_session_id(source: str) -> str:
    safe = _UNSAFE_SESSION_CHARS.sub("_", source) or "unknown"
    return f"mqtt-{safe}"[:128]


class MqttBridge:
    """Subscribes to `topic_in`, answers each event, publishes to `topic_out`."""

    def __init__(
        self,
        config: MqttConfig | None = None,
        orchestrator: Orchestrator | None = None,
        session_store: SessionStore | None = None,
        audit_log: AuditLog | None = None,
    ) -> None:
        self.config = config or MqttConfig()
        self.orchestrator = orchestrator or Orchestrator()
        self.session_store = session_store or JSONFileSessionStore()
        self.audit_log = audit_log or NullAuditLog()

        self.client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=self.config.client_id,
        )
        if self.config.username:
            password = self.config.password.get_secret_value() if self.config.password else None
            self.client.username_pw_set(self.config.username, password)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message

    # -- connection lifecycle -------------------------------------------------

    def connect(self) -> None:
        self.client.connect(self.config.host, self.config.port, keepalive=self.config.keepalive)

    def loop_forever(self) -> None:
        self.connect()
        self.client.loop_forever()

    def disconnect(self) -> None:
        self.client.disconnect()

    # -- paho callbacks ---------------------------------------------------------

    def _on_connect(
        self, client: mqtt.Client, userdata: object, flags, reason_code, properties
    ) -> None:
        client.subscribe(self.config.topic_in)

    def _on_message(self, client: mqtt.Client, userdata: object, msg: mqtt.MQTTMessage) -> None:
        try:
            event, session_id = self.parse_message(msg.payload)
        except (ValueError, KeyError, TypeError) as exc:
            logger.warning("Ignoring unparseable MQTT message on %s: %s", msg.topic, exc)
            return

        decision = self.handle_event(event, session_id)
        client.publish(self.config.topic_out, decision.model_dump_json())

    # -- pure logic: directly testable without a real broker ---------------------

    @staticmethod
    def parse_message(payload: bytes | str) -> tuple[VisitorEvent, str]:
        """Parse a raw MQTT payload into a `VisitorEvent` and its session id."""
        data = json.loads(payload)
        text = data["text"]
        source = data.get("source", "mqtt")
        timestamp = data.get("timestamp")
        event_time = datetime.now(UTC) if timestamp is None else datetime.fromisoformat(timestamp)
        session_id = data.get("session_id") or _default_session_id(source)
        event = VisitorEvent(source=source, text=text, timestamp=event_time)
        return event, session_id

    def handle_event(self, event: VisitorEvent, session_id: str) -> ResponseDecision:
        state = self.session_store.load(session_id) or SessionState(
            session_id=session_id, updated_at=event.timestamp
        )
        decision = self.orchestrator.respond(event, state=state, audit_log=self.audit_log)
        self.session_store.save(state)
        return decision


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    orchestrator = Orchestrator(llm_provider=OllamaProvider(config=OllamaConfig.from_env()))
    bridge = MqttBridge(config=MqttConfig.from_env(), orchestrator=orchestrator)
    logger.info(
        "Starting KNOCK MQTT bridge: %s:%s (in=%s, out=%s)",
        bridge.config.host,
        bridge.config.port,
        bridge.config.topic_in,
        bridge.config.topic_out,
    )
    bridge.loop_forever()


if __name__ == "__main__":
    main()
