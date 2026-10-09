"""Frigate bridge: feeds zone-entry detections into the orchestrator.

Frigate (https://frigate.video) already runs its own object detector and
publishes results over MQTT, so this bridge gets "vision" (person/vehicle/
package labels) for free from Frigate's event payload -- no separate model
needed for that part. It only reacts to `frigate/events` messages of type
"new" whose `entered_zones` is non-empty (an object actually crossed into a
defined zone, e.g. "front_porch"), not just any detection visible anywhere
in frame -- this avoids false triggers from someone merely passing by on the
sidewalk. `trigger_labels` (default `["person"]`) and `zones` (default: any
zone) further narrow what counts as a trigger.

If a `VisionProvider` is supplied, a matching event's snapshot is fetched
from Frigate's HTTP API and described, with the description folded into the
VisitorEvent's text; vision is optional enrichment, not a dependency -- a
failure there is logged and the plain detection still goes through.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
import paho.mqtt.client as mqtt

from knock.config import FrigateConfig, OllamaConfig, VisionConfig
from knock.core.audit import AuditLog, JSONLAuditLog, NullAuditLog
from knock.core.events import VisitorEvent
from knock.core.orchestrator import Orchestrator
from knock.core.responses import ResponseDecision
from knock.core.scene import SceneContext
from knock.core.session_store import JSONFileSessionStore, SessionStore
from knock.core.state import SessionState
from knock.providers.llm.ollama import OllamaProvider
from knock.providers.vision.base import VisionProvider, observe_scene
from knock.providers.vision.ollama import OllamaVisionProvider

logger = logging.getLogger(__name__)

_UNSAFE_SESSION_CHARS = re.compile(r"[^A-Za-z0-9_-]")


def _default_session_id(camera: str) -> str:
    safe = _UNSAFE_SESSION_CHARS.sub("_", camera) or "unknown"
    return f"frigate-{safe}"[:128]


@dataclass(frozen=True)
class FrigateDetection:
    """A single Frigate object detection that crossed KNOCK's trigger bar."""

    event_id: str
    camera: str
    label: str
    zones: tuple[str, ...]
    has_snapshot: bool


class FrigateBridge:
    """Subscribes to `<topic_prefix>/events`, answers matching detections."""

    def __init__(
        self,
        config: FrigateConfig | None = None,
        orchestrator: Orchestrator | None = None,
        session_store: SessionStore | None = None,
        audit_log: AuditLog | None = None,
        vision_provider: VisionProvider | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.config = config or FrigateConfig()
        self.orchestrator = orchestrator or Orchestrator()
        self.session_store = session_store or JSONFileSessionStore()
        self.audit_log = audit_log or NullAuditLog()
        self.vision_provider = vision_provider
        self._http_client = http_client or httpx.Client(timeout=10.0)

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
        self.client.connect(
            self.config.mqtt_host, self.config.mqtt_port, keepalive=self.config.keepalive
        )

    def loop_forever(self) -> None:
        self.connect()
        self.client.loop_forever()

    def disconnect(self) -> None:
        self.client.disconnect()

    # -- paho callbacks ---------------------------------------------------------

    def _on_connect(
        self, client: mqtt.Client, userdata: object, flags, reason_code, properties
    ) -> None:
        client.subscribe(f"{self.config.topic_prefix}/events")

    def _on_message(self, client: mqtt.Client, userdata: object, msg: mqtt.MQTTMessage) -> None:
        try:
            detection = self.parse_event(msg.payload)
        except (ValueError, KeyError, TypeError) as exc:
            logger.warning("Ignoring unparseable Frigate message on %s: %s", msg.topic, exc)
            return

        if detection is None:
            return

        decision = self.handle_detection(detection)
        client.publish(self.config.topic_out, decision.model_dump_json())

    # -- pure logic: directly testable without a real broker ---------------------

    def parse_event(self, payload: bytes | str) -> FrigateDetection | None:
        """Parse a `frigate/events` payload; `None` if it doesn't meet the trigger bar."""
        data = json.loads(payload)
        if data.get("type") != "new":
            return None

        after = data["after"]
        label = after.get("label")
        entered_zones = tuple(after.get("entered_zones") or ())

        if label not in self.config.trigger_labels:
            return None
        if not entered_zones:
            return None
        if self.config.zones and not any(zone in self.config.zones for zone in entered_zones):
            return None

        return FrigateDetection(
            event_id=after["id"],
            camera=after["camera"],
            label=label,
            zones=entered_zones,
            has_snapshot=bool(after.get("has_snapshot")),
        )

    def handle_detection(self, detection: FrigateDetection) -> ResponseDecision:
        zones_text = ", ".join(detection.zones)
        text = f"{detection.label} detected entering {zones_text} on {detection.camera}"
        # Frigate's own detector output is free, structured scene context;
        # the vision model's observation (when available) is layered on top.
        # Both stay out of `text` -- see knock.core.scene for why.
        scene = SceneContext(
            camera=detection.camera,
            detected_labels=[detection.label],
            zones=list(detection.zones),
            captured_at=datetime.now(UTC),
        )
        snapshot: bytes | None = None

        if self.vision_provider is not None and detection.has_snapshot:
            try:
                snapshot = self._fetch_snapshot(detection.event_id)
                scene = scene.model_copy(
                    update={"observation": observe_scene(self.vision_provider, snapshot)}
                )
            except Exception as exc:  # noqa: BLE001 - vision enrichment is best-effort
                logger.warning("Vision enrichment failed for event %s: %s", detection.event_id, exc)

        session_id = _default_session_id(detection.camera)
        event = VisitorEvent(
            source=f"frigate-{detection.camera}",
            text=text,
            timestamp=datetime.now(UTC),
            scene=scene,
        )
        state = self.session_store.load(session_id) or SessionState(
            session_id=session_id, updated_at=event.timestamp
        )
        decision = self.orchestrator.respond(
            event, state=state, audit_log=self.audit_log, image=snapshot
        )
        self.session_store.save(state)
        return decision

    def _fetch_snapshot(self, event_id: str) -> bytes:
        url = f"http://{self.config.http_host}:{self.config.http_port}/api/events/{event_id}/snapshot.jpg"
        response = self._http_client.get(url)
        response.raise_for_status()
        return response.content


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    vision_config = VisionConfig.from_env()
    llm_provider = OllamaProvider(config=OllamaConfig.from_env())
    vision_provider = OllamaVisionProvider(config=vision_config)
    # Load both models now rather than on the first detection -- see warm_up().
    llm_provider.warm_up()
    vision_provider.warm_up()
    orchestrator = Orchestrator(
        llm_provider=llm_provider, send_image_to_brain=vision_config.send_image_to_brain
    )
    bridge = FrigateBridge(
        config=FrigateConfig.from_env(),
        orchestrator=orchestrator,
        vision_provider=vision_provider,
        # Without this, the bridge falls back to its class default
        # (NullAuditLog) and every real conversation silently never reaches
        # audit.jsonl / the web UI's History page -- the CLI and web API
        # already construct a real JSONLAuditLog() the same way.
        audit_log=JSONLAuditLog(),
    )
    logger.info(
        "Starting KNOCK Frigate bridge: %s:%s (prefix=%s, labels=%s)",
        bridge.config.mqtt_host,
        bridge.config.mqtt_port,
        bridge.config.topic_prefix,
        bridge.config.trigger_labels,
    )
    bridge.loop_forever()


if __name__ == "__main__":
    main()
