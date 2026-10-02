"""UniFi Protect bridge: feeds doorbell-ring (and optional smart-detect) events
into the orchestrator.

Authenticates against a local UniFi OS console via an API key created for a
local console user -- cloud SSO/MFA accounts are explicitly not supported by
the underlying `uiprotect` library, so a local-only account is required,
which actually fits KNOCK's local-first stance well. Subscribes to
Protect's realtime event websocket and reacts to a doorbell `ring` by
default; `trigger_on` can be extended with smart-detect object types (e.g.
"person", "package") to also react to those.

UniFi itself provides no speech-to-text, so the VisitorEvent's text is a
generic trigger description -- real visitor speech is a future audio-
pipeline concern. If a `VisionProvider` is supplied, the triggering
camera's snapshot is fetched and described, folded into the event text;
this is optional enrichment -- a failure there is logged and the plain
trigger still goes through.

If a `TTSProvider` is supplied, the orchestrator's response text is
synthesized and streamed out to the triggering camera's speaker (UniFi's
"talkback" feature -- many Protect doorbells can play audio back at the
visitor) via `uiprotect.stream.TalkbackStream`. This is the genuinely
two-way part: KNOCK can both hear/see the visitor (via vision) and speak
back to them. Like vision, talkback is optional and best-effort -- a
camera with no speaker, or any streaming failure, is logged and does not
affect the rest of the response.

The `uiprotect` event callback is synchronous (it's called directly by the
library's websocket handler), but snapshot/vision/talkback all need async
I/O, so the sync callback (`_on_event`) just schedules the real async
handling (`handle_event`) onto the running event loop rather than blocking
it.
"""

from __future__ import annotations

import asyncio
import logging
import re
import tempfile
import wave
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from uiprotect import EventChange, ProtectApiClient
from uiprotect.stream import TalkbackStream

from knock.config import UnifiConfig
from knock.core.audit import AuditLog, NullAuditLog
from knock.core.events import VisitorEvent
from knock.core.orchestrator import Orchestrator
from knock.core.responses import ResponseDecision
from knock.core.session_store import JSONFileSessionStore, SessionStore
from knock.core.state import SessionState
from knock.providers.tts.base import SynthesizedAudio, TTSProvider
from knock.providers.vision.base import VisionProvider

logger = logging.getLogger(__name__)

_UNSAFE_SESSION_CHARS = re.compile(r"[^A-Za-z0-9_-]")
_SMART_DETECT_EVENT_TYPES = {"smartDetectZone", "smartDetectLine", "smartDetectLoiterZone"}


def _default_session_id(device_id: str) -> str:
    safe = _UNSAFE_SESSION_CHARS.sub("_", device_id) or "unknown"
    return f"unifi-{safe}"[:128]


def _write_wav(path: Path, audio: SynthesizedAudio) -> None:
    """Wrap raw PCM in a WAV header so PyAV (via `TalkbackStream`) can read it."""
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(audio.channels)
        wav_file.setsampwidth(audio.width)
        wav_file.setframerate(audio.rate)
        wav_file.writeframes(audio.audio)


class _ProtectEventLike(Protocol):
    """The subset of `uiprotect.ProtectEvent` this bridge actually uses.

    Lets tests construct a plain stand-in instead of a real `ProtectEvent`
    (whose pydantic model construction has internal library requirements
    well beyond what KNOCK's own logic touches).
    """

    @property
    def type(self) -> Any: ...

    @property
    def device_id(self) -> str: ...

    @property
    def smart_detect_types(self) -> Sequence[Any]: ...


class UnifiBridge:
    """Subscribes to Protect's event websocket, answers matching triggers."""

    def __init__(
        self,
        config: UnifiConfig | None = None,
        orchestrator: Orchestrator | None = None,
        session_store: SessionStore | None = None,
        audit_log: AuditLog | None = None,
        vision_provider: VisionProvider | None = None,
        tts_provider: TTSProvider | None = None,
        client: ProtectApiClient | None = None,
        talkback_stream_factory: Any = TalkbackStream,
    ) -> None:
        self.config = config or UnifiConfig()
        self.orchestrator = orchestrator or Orchestrator()
        self.session_store = session_store or JSONFileSessionStore()
        self.audit_log = audit_log or NullAuditLog()
        self.vision_provider = vision_provider
        self.tts_provider = tts_provider
        self.client = client or ProtectApiClient(
            host=self.config.host,
            port=self.config.port,
            api_key=self.config.api_key,
            verify_ssl=self.config.verify_ssl,
        )
        self._talkback_stream_factory = talkback_stream_factory

    # -- pure logic: directly testable without a real console -----------------

    def should_trigger(self, event: _ProtectEventLike) -> bool:
        event_type = str(event.type)
        if event_type in self.config.trigger_on:
            return True
        if event_type in _SMART_DETECT_EVENT_TYPES:
            smart_types = {str(t) for t in event.smart_detect_types}
            return bool(smart_types & set(self.config.trigger_on))
        return False

    def build_event(self, event: _ProtectEventLike) -> VisitorEvent:
        event_type = str(event.type)
        if event_type in _SMART_DETECT_EVENT_TYPES and event.smart_detect_types:
            types_text = ", ".join(str(t) for t in event.smart_detect_types)
            text = f"{types_text} detected on {event.device_id}"
        else:
            text = f"Doorbell {event_type} on {event.device_id}"
        return VisitorEvent(
            source=f"unifi-{event.device_id}", text=text, timestamp=datetime.now(UTC)
        )

    async def handle_event(self, event: _ProtectEventLike) -> ResponseDecision | None:
        if not self.should_trigger(event):
            return None

        visitor_event = self.build_event(event)

        if self.vision_provider is not None:
            try:
                snapshot = await self.client.get_camera_snapshot(event.device_id)
                if snapshot is not None:
                    description = await asyncio.to_thread(self.vision_provider.describe, snapshot)
                    visitor_event = visitor_event.model_copy(
                        update={"text": f"{visitor_event.text}: {description}"}
                    )
            except Exception as exc:  # noqa: BLE001 - vision enrichment is best-effort
                logger.warning("Vision enrichment failed for device %s: %s", event.device_id, exc)

        session_id = _default_session_id(event.device_id)
        state = self.session_store.load(session_id) or SessionState(
            session_id=session_id, updated_at=visitor_event.timestamp
        )
        decision = self.orchestrator.respond(visitor_event, state=state, audit_log=self.audit_log)
        self.session_store.save(state)

        if self.tts_provider is not None:
            await self.speak_to_visitor(event.device_id, decision.text)

        return decision

    async def speak_to_visitor(self, device_id: str, text: str) -> None:
        """Synthesize `text` and stream it to `device_id`'s speaker, best-effort.

        Any failure here (no speaker on this camera, synthesis error,
        streaming error) is logged and swallowed -- talkback is an
        enhancement on top of the text response, never a requirement for it.
        """
        if self.tts_provider is None:
            return

        try:
            audio = await self.tts_provider.synthesize(text)
            camera = await self.client.get_camera(device_id)
            if not camera.feature_flags.has_speaker:
                logger.info("Camera %s has no speaker; skipping talkback", device_id)
                return

            with tempfile.TemporaryDirectory() as tmp_dir:
                audio_path = Path(tmp_dir) / "response.wav"
                _write_wav(audio_path, audio)

                session = None
                try:
                    session = await self.client.create_talkback_session_public(device_id)
                except Exception as exc:  # noqa: BLE001 - fall back to local talkback settings
                    logger.debug(
                        "No public talkback session for %s (%s); using local settings",
                        device_id,
                        exc,
                    )

                stream = self._talkback_stream_factory(camera, str(audio_path), session)
                await stream.run_until_complete()
        except Exception as exc:  # noqa: BLE001 - talkback is best-effort
            logger.warning("Talkback to device %s failed: %s", device_id, exc)

    # -- uiprotect callback ----------------------------------------------------

    def _on_event(self, event: _ProtectEventLike, change: EventChange) -> None:
        if change != EventChange.STARTED:
            return
        asyncio.get_running_loop().create_task(self.handle_event(event))

    # -- connection lifecycle -------------------------------------------------

    async def run(self) -> None:
        unsubscribe = await self.client.subscribe_events_and_prime(self._on_event)
        try:
            await asyncio.Event().wait()
        finally:
            unsubscribe()
            await self.client.close_session()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    bridge = UnifiBridge(config=UnifiConfig.from_env())
    logger.info(
        "Starting KNOCK UniFi Protect bridge: %s:%s (trigger_on=%s)",
        bridge.config.host,
        bridge.config.port,
        bridge.config.trigger_on,
    )
    asyncio.run(bridge.run())


if __name__ == "__main__":
    main()
