"""UniFi Protect bridge: feeds doorbell-ring (and optional smart-detect) events
into the orchestrator.

Authenticates against a local UniFi OS console via an API key created for a
local console user -- cloud SSO/MFA accounts are explicitly not supported by
the underlying `uiprotect` library, so a local-only account is required,
which actually fits KNOCK's local-first stance well. Subscribes to
Protect's realtime event websocket and reacts to a doorbell `ring` by
default; `trigger_on` can be extended with smart-detect object types (e.g.
"person", "package") to also react to those.

If a `STTProvider` (e.g. `WhisperSTTProvider`) is supplied, KNOCK instead
*listens*: it opens the triggering camera's RTSPS stream, captures a short
window of audio from its microphone, and transcribes it, using the real
transcript as the VisitorEvent's text. Without an STT provider, the text
stays a generic trigger description (e.g. "Doorbell ring on cam1"). If a
`VisionProvider` is supplied, the triggering camera's snapshot is fetched
and described too, appended to whichever text (transcript or placeholder)
is already in play. Both are optional enrichment -- a failure in either is
logged and the rest of the response still goes through.

If a `TTSProvider` is supplied, the orchestrator's response text is
synthesized and streamed out to the triggering camera's speaker (UniFi's
"talkback" feature -- many Protect doorbells can play audio back at the
visitor) via `uiprotect.stream.TalkbackStream`. Combined with STT, this is
the genuinely two-way part: KNOCK can hear what the visitor actually said
and speak a response back. Like vision, talkback is optional and
best-effort -- a camera with no speaker, or any streaming failure, is
logged and does not affect the rest of the response.

The `uiprotect` event callback is synchronous (it's called directly by the
library's websocket handler), but snapshot/speech/vision/talkback all need
async I/O, so the sync callback (`_on_event`) just schedules the real
async handling (`handle_event`) onto the running event loop rather than
blocking it.
"""

from __future__ import annotations

import asyncio
import logging
import re
import tempfile
import time
import wave
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

import av
from uiprotect import EventChange, ProtectApiClient
from uiprotect.stream import TalkbackStream

from knock.config import (
    HomeAssistantConfig,
    KokoroConfig,
    OllamaConfig,
    UnifiConfig,
    VisionConfig,
    WhisperConfig,
)
from knock.conversation.prompts import GREETING
from knock.core.audit import AuditLog, NullAuditLog
from knock.core.events import VisitorEvent
from knock.core.orchestrator import Orchestrator
from knock.core.responses import ResponseDecision
from knock.core.session_store import JSONFileSessionStore, SessionStore
from knock.core.state import SessionState
from knock.integrations.homeassistant import (
    ACTION_DEVICE_ID_SEP,
    KNOCK_ON_MY_WAY_ACTION,
    KNOCK_TURN_AWAY_ACTION,
    HomeAssistantActionListener,
    HomeAssistantNotifier,
)
from knock.providers.llm.ollama import OllamaProvider
from knock.providers.stt.base import STTProvider
from knock.providers.stt.whisper import WhisperSTTProvider
from knock.providers.tts.base import SynthesizedAudio, TTSProvider
from knock.providers.tts.kokoro import KokoroTTSProvider
from knock.providers.vision.base import VisionProvider
from knock.providers.vision.ollama import OllamaVisionProvider

logger = logging.getLogger(__name__)

_UNSAFE_SESSION_CHARS = re.compile(r"[^A-Za-z0-9_-]")
_SMART_DETECT_EVENT_TYPES = {"smartDetectZone", "smartDetectLine", "smartDetectLoiterZone"}
_CAPTURE_SAMPLE_RATE = 16000

# A per-camera session has no expiry on its own -- without this, a greeting
# meant to play once per *visit* would instead play exactly once ever for a
# given camera, never again for any later, unrelated visitor. Two distinct
# triggers for a fresh visit: an explicit `ring` (a visitor pressing the
# button is as clear a "this is a new visit" signal as exists -- always
# restarts), or enough idle time since the session's last turn that whatever
# was happening before is almost certainly over.
_SESSION_IDLE_TIMEOUT = timedelta(minutes=5)

# Safety cap on how many listen/respond round-trips one visit can run,
# regardless of how talkative the visitor is -- end-of-conversation
# detection (see `handle_event`) is "the visitor went quiet," which is a
# good default but not a guarantee against e.g. background noise keeping
# Whisper returning short junk transcripts forever.
_MAX_CONVERSATION_TURNS = 5

# Spoken right after listening ends and before the (sometimes slow --
# vision description, an LLM fallback call) processing that decides the
# actual reply, so the visitor gets some acknowledgement instead of dead
# air while that runs.
_THINKING_PHRASE = "Let me think about that for a moment."

# Spoken back to the visitor once someone at the Home Assistant end taps an
# action button on a signature-required delivery notification (see
# `handle_notification_action`) -- the visitor is presumably still standing
# at the door waiting to hear whether to stick around.
_ON_MY_WAY_PHRASE = "Good news, the homeowner says they're on their way."
_TURN_AWAY_PHRASE = "I'm sorry, but the homeowner isn't able to accept this right now."


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


def _capture_rtsp_audio(
    rtsp_url: str, duration: float, sample_rate: int, verify_ssl: bool = True
) -> bytes:
    """Blocking: decode up to `duration` seconds of mono 16-bit PCM from an
    RTSP(S) stream's audio track.

    Runs in a worker thread (via `asyncio.to_thread`) since PyAV's decode
    loop is itself blocking -- same reasoning `uiprotect`'s own
    `TalkbackStream` uses for the outbound direction.
    """
    resampler = av.AudioResampler(format="s16", layout="mono", rate=sample_rate)
    chunks: list[bytes] = []
    start = time.monotonic()

    options = {"rtsp_transport": "tcp"}
    if not verify_ssl:
        # UniFi's local console uses a self-signed cert for RTSPS by default
        # -- same reasoning as `ProtectApiClient(verify_ssl=...)` for the
        # HTTPS API, just threaded through to FFmpeg's TLS layer instead of
        # aiohttp's. Without this, `av.open()` fails outright with "[Errno
        # 5] Input/output error"; FFmpeg only logs the real reason ("Peer
        # certificate failed verification") at VERBOSE level, which that
        # exception's message never surfaces.
        options["tls_verify"] = "0"

    with av.open(rtsp_url, timeout=(5.0, 5.0), options=options) as container:
        if not container.streams.audio:
            raise RuntimeError(f"RTSP stream has no audio track: {rtsp_url}")
        audio_stream = container.streams.audio[0]

        for frame in container.decode(audio_stream):
            for resampled in resampler.resample(frame):
                chunks.append(bytes(resampled.planes[0]))
            if time.monotonic() - start >= duration:
                break

    return b"".join(chunks)


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
        stt_provider: STTProvider | None = None,
        client: ProtectApiClient | None = None,
        talkback_stream_factory: Any = TalkbackStream,
        rtsp_audio_capture: Callable[[str, float, int, bool], bytes] = _capture_rtsp_audio,
        ha_notifier: HomeAssistantNotifier | None = None,
    ) -> None:
        self.config = config or UnifiConfig()
        self.orchestrator = orchestrator or Orchestrator()
        self.session_store = session_store or JSONFileSessionStore()
        self.audit_log = audit_log or NullAuditLog()
        self.vision_provider = vision_provider
        self.tts_provider = tts_provider
        self.stt_provider = stt_provider
        # Optional: lets the household get an actual phone notification for
        # situations that need a human, not just a spoken reply at the door
        # -- today, only a signature-required delivery (see handle_event).
        # None by default (not every UniFi deployment also runs Home
        # Assistant); notify() itself is already a no-op without a
        # configured notify_service, so this stays harmless either way.
        self.ha_notifier = ha_notifier
        self.client = client or ProtectApiClient(
            host=self.config.host,
            port=self.config.port,
            api_key=self.config.api_key.get_secret_value(),
            verify_ssl=self.config.verify_ssl,
        )
        self._talkback_stream_factory = talkback_stream_factory
        self._rtsp_audio_capture = rtsp_audio_capture

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

    def _start_or_resume_session(
        self, session_id: str, event: _ProtectEventLike, timestamp: datetime
    ) -> SessionState:
        """A fresh `SessionState` for a new visit, or the persisted one if
        this still looks like the same ongoing visit.

        See `_SESSION_IDLE_TIMEOUT`'s docstring for why a per-camera session
        can't just persist forever.
        """
        existing = self.session_store.load(session_id)
        is_ring = str(event.type) == "ring"
        is_stale = (
            existing is not None and (timestamp - existing.updated_at) > _SESSION_IDLE_TIMEOUT
        )

        if existing is None or is_ring or is_stale:
            return SessionState(session_id=session_id, updated_at=timestamp)
        return existing

    async def _notify_household(
        self, device_id: str, message: str, *, actions: list[dict[str, str]] | None = None
    ) -> None:
        """Best-effort, off the event loop (the HTTP call itself is
        synchronous) -- a down or unconfigured Home Assistant never affects
        the rest of the response.
        """
        if self.ha_notifier is None:
            return
        try:
            await asyncio.to_thread(self.ha_notifier.notify, message, actions=actions)
        except Exception as exc:  # noqa: BLE001 - notification is best-effort
            logger.warning("Home Assistant notify failed for device %s: %s", device_id, exc)

    async def handle_event(self, event: _ProtectEventLike) -> ResponseDecision | None:
        if not self.should_trigger(event):
            return None

        visitor_event = self.build_event(event)
        session_id = _default_session_id(event.device_id)
        state = self._start_or_resume_session(session_id, event, visitor_event.timestamp)
        is_first_turn = state.turn_count == 0
        # Only true once the greeting was actually spoken aloud below -- a
        # text-only deployment (no tts_provider) still needs the orchestrator
        # to put it in the returned text, since nothing else ever said it.
        greeted_aloud = is_first_turn and self.tts_provider is not None

        # A real doorbell intercom answers before waiting for the visitor to
        # speak into silence -- greet first (once per session), *then*
        # listen, rather than blindly capturing audio with nothing said to
        # prompt a reply. `suppress_greeting` below tells the orchestrator
        # not to also prepend it to the spoken response, since it was
        # already said out loud here.
        if greeted_aloud:
            await self.speak_to_visitor(event.device_id, GREETING)

        if self.stt_provider is not None:
            transcript = await self.listen_to_visitor(event.device_id)
            if transcript:
                visitor_event = visitor_event.model_copy(update={"text": transcript})
            if self.tts_provider is not None:
                await self.speak_to_visitor(event.device_id, _THINKING_PHRASE)

        if self.vision_provider is not None:
            try:
                snapshot = await self.client.get_public_api_camera_snapshot(event.device_id)
                if snapshot is not None:
                    description = await asyncio.to_thread(self.vision_provider.describe, snapshot)
                    visitor_event = visitor_event.model_copy(
                        update={"text": f"{visitor_event.text}: {description}"}
                    )
            except Exception as exc:  # noqa: BLE001 - vision enrichment is best-effort
                logger.warning("Vision enrichment failed for device %s: %s", event.device_id, exc)

        # The conversation itself: respond, speak it, then listen for a
        # reply and keep going as long as the visitor keeps talking. Ends
        # when they go quiet (an empty transcript -- they've said what they
        # came to say, or left), an emergency escalates (nothing more to
        # resolve automatically), there's no STT to listen with or no TTS
        # to have said anything worth replying to in the first place
        # (always one-shot), or the turn cap is hit.
        can_converse = self.stt_provider is not None and self.tts_provider is not None
        decision: ResponseDecision | None = None
        for turn_index in range(_MAX_CONVERSATION_TURNS):
            decision = self.orchestrator.respond(
                visitor_event,
                state=state,
                audit_log=self.audit_log,
                suppress_greeting=greeted_aloud,
            )
            self.session_store.save(state)

            # An emergency alert goes out immediately -- ahead of speaking,
            # since TTS synthesis/streaming takes real seconds an urgent
            # notification shouldn't wait on. A signature-required delivery
            # isn't urgent the same way: speak the "give me a sec"
            # acknowledgement first, *then* actually go notify, matching
            # what was just said out loud.
            if decision.escalate:
                await self._notify_household(
                    event.device_id,
                    f'Possible emergency at the door: "{visitor_event.text}"',
                )

            if self.tts_provider is not None:
                await self.speak_to_visitor(event.device_id, decision.text)

            if decision.intent == "delivery_signature_required":
                fallback = f"A delivery at the door needs a signature (camera: {event.device_id})."
                summary = self.orchestrator.summarize_for_notification(
                    visitor_event.text, fallback=fallback
                )
                on_my_way = f"{KNOCK_ON_MY_WAY_ACTION}{ACTION_DEVICE_ID_SEP}{event.device_id}"
                turn_away = f"{KNOCK_TURN_AWAY_ACTION}{ACTION_DEVICE_ID_SEP}{event.device_id}"
                await self._notify_household(
                    event.device_id,
                    summary,
                    # The device id rides along in the action identifier
                    # itself (parsed back out by HomeAssistantActionListener)
                    # -- the simplest way to know which camera a button tap
                    # refers to, without depending on whatever extra context
                    # a given Home Assistant mobile app version does or
                    # doesn't echo back on the action event.
                    actions=[
                        {"action": on_my_way, "title": "I'm on my way"},
                        {"action": turn_away, "title": "Turn them away"},
                    ],
                )

            is_last_possible_turn = turn_index == _MAX_CONVERSATION_TURNS - 1
            if decision.escalate or not can_converse or is_last_possible_turn:
                break

            reply = await self.listen_to_visitor(event.device_id)
            if not reply:
                break
            if self.tts_provider is not None:
                await self.speak_to_visitor(event.device_id, _THINKING_PHRASE)
            visitor_event = visitor_event.model_copy(
                update={"text": reply, "timestamp": datetime.now(UTC)}
            )

        return decision

    async def handle_notification_action(self, action_id: str, device_id: str) -> None:
        """React to a Home Assistant mobile app notification-action tap --
        the "I'm on my way" / "Turn them away" buttons on a
        signature-required delivery notification (see `handle_event`).

        Called by `HomeAssistantActionListener` as its `on_action` callback
        (see `main()`), which has already parsed the device id back out of
        the action identifier itself.
        """
        if action_id == KNOCK_ON_MY_WAY_ACTION:
            await self.speak_to_visitor(device_id, _ON_MY_WAY_PHRASE)
        elif action_id == KNOCK_TURN_AWAY_ACTION:
            await self.speak_to_visitor(device_id, _TURN_AWAY_PHRASE)
        else:
            logger.warning(
                "Unrecognized notification action %r for device %s", action_id, device_id
            )

    async def listen_to_visitor(self, device_id: str) -> str:
        """Capture a short audio window from `device_id`'s mic and transcribe it.

        Best-effort like vision/talkback: no RTSPS stream available, no
        audio track, or any capture/transcription failure returns "" rather
        than raising, so the caller falls back to the generic trigger text.
        """
        if self.stt_provider is None:
            return ""

        try:
            # `UnifiConfig` carries only an api_key (no username/password), so
            # `self.client` is a public-only `ProtectApiClient` -- the private
            # `get_camera()`/`Camera.get_rtsps_streams()` calls raise
            # `PublicOnlyModeError` on it. `public_bootstrap.cameras` is kept
            # primed by `update_public()` (called once in `run()`, before any
            # event can fire), including each camera's `rtsps_streams`.
            camera = self.client.public_bootstrap.cameras.get(device_id)
            streams = camera.rtsps_streams if camera is not None else None
            url = streams.get_stream_url(self.config.rtsp_quality) if streams else None
            if not url:
                logger.info("No RTSPS stream available for %s; skipping speech capture", device_id)
                return ""

            pcm = await asyncio.to_thread(
                self._rtsp_audio_capture,
                url,
                self.config.listen_seconds,
                _CAPTURE_SAMPLE_RATE,
                self.config.verify_ssl,
            )
            if not pcm:
                return ""

            return await self.stt_provider.transcribe(
                pcm, rate=_CAPTURE_SAMPLE_RATE, width=2, channels=1
            )
        except Exception as exc:  # noqa: BLE001 - speech capture is best-effort
            logger.warning("Speech capture failed for device %s: %s", device_id, exc)
            return ""

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
            # Same public-only-client constraint as `listen_to_visitor` --
            # read the already-primed camera from `public_bootstrap` rather
            # than the private `get_camera()`.
            camera = self.client.public_bootstrap.cameras.get(device_id)
            if camera is None or not camera.feature_flags.has_speaker:
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


async def _run_bridge_and_listener(bridge: UnifiBridge, ha_config: HomeAssistantConfig) -> None:
    tasks = [bridge.run()]
    # The listener opens its own persistent websocket connection -- only
    # worth running (and worth the connection-refused log spam if Home
    # Assistant isn't reachable) when a token is actually configured, same
    # gate `ha_notifier` implicitly gets from `notify()`'s own no-op check.
    if ha_config.token.get_secret_value():
        listener = HomeAssistantActionListener(
            config=ha_config, on_action=bridge.handle_notification_action
        )
        tasks.append(listener.run())
    await asyncio.gather(*tasks)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    orchestrator = Orchestrator(llm_provider=OllamaProvider(config=OllamaConfig.from_env()))
    ha_config = HomeAssistantConfig.from_env()
    bridge = UnifiBridge(
        config=UnifiConfig.from_env(),
        orchestrator=orchestrator,
        vision_provider=OllamaVisionProvider(config=VisionConfig.from_env()),
        stt_provider=WhisperSTTProvider(config=WhisperConfig.from_env()),
        tts_provider=KokoroTTSProvider(config=KokoroConfig.from_env()),
        # A no-op if KNOCK_HA_NOTIFY_SERVICE isn't set -- no need to check
        # whether Home Assistant is actually configured before wiring it in.
        ha_notifier=HomeAssistantNotifier(config=ha_config),
    )
    logger.info(
        "Starting KNOCK UniFi Protect bridge: %s:%s (trigger_on=%s)",
        bridge.config.host,
        bridge.config.port,
        bridge.config.trigger_on,
    )
    asyncio.run(_run_bridge_and_listener(bridge, ha_config))


if __name__ == "__main__":
    main()
