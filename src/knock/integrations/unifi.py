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

A Protect "package" smart-detect (the doorbell's package camera saw a box)
never starts a conversation -- there's nobody to talk to. It sends a
household notification with the package-camera photo and a "Got it" button
instead, held back until the visitor walks away -- plus optional reminders
and "gone without a Got it" alerts. See `integrations/package_alerts.py`.

The `uiprotect` event callback is synchronous (it's called directly by the
library's websocket handler), but snapshot/speech/vision/talkback all need
async I/O, so the sync callback (`_on_event`) just schedules the real
async handling (`handle_event`) onto the running event loop rather than
blocking it.

A knock detected by an external, non-Protect sensor (e.g. a vibration
sensor bridged through Home Assistant -- see
`homeassistant.HomeAssistantKnockListener`) gets the exact same
conversation flow through `handle_knock`, not a separate, thinner path:
a knock deserves the same greeting/listening/camera-context/spoken-reply
experience a doorbell ring gets, just from a different trigger source.
"""

from __future__ import annotations

import array
import asyncio
import functools
import io
import logging
import re
import tempfile
import time
import uuid
import wave
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import cache
from importlib import resources
from pathlib import Path
from typing import Any, Protocol

import av
from uiprotect import EventChange, ProtectApiClient
from uiprotect.stream import TalkbackStream

from knock.config import (
    DoorsConfig,
    HomeAssistantConfig,
    KnockConfig,
    KokoroConfig,
    OllamaConfig,
    SipConfig,
    UnifiConfig,
    VisionConfig,
    WhisperConfig,
)
from knock.core.audit import AuditLog, JSONLAuditLog, NullAuditLog
from knock.core.conversation_runner import ConversationHooks, run_conversation_loop
from knock.core.doors import Door, DoorRegistry, resolve_doors
from knock.core.events import VisitorEvent
from knock.core.orchestrator import Orchestrator
from knock.core.package_log import JSONLPackageLog, NullPackageLog, PackageLog
from knock.core.responses import ResponseDecision
from knock.core.scene import SceneContext, SceneObservation
from knock.core.session_store import JSONFileSessionStore, SessionStore
from knock.core.snapshot_store import SnapshotStore
from knock.core.state import SessionState
from knock.core.systems_check import (
    is_systems_check_trigger,
    run_systems_check,
    summarize_systems_check,
)
from knock.core.tool_calling_detection import resolve_tool_calling
from knock.integrations.homeassistant import (
    KNOCK_COMING_TO_DOOR_ACTION,
    KNOCK_ON_MY_WAY_ACTION,
    KNOCK_PACKAGE_ACK_ACTION,
    KNOCK_TURN_AWAY_ACTION,
    HomeAssistantActionListener,
    HomeAssistantKnockListener,
    HomeAssistantNotifier,
    NotificationCategory,
)
from knock.integrations.household import HouseholdNotifier
from knock.integrations.package_alerts import (
    PackageAlerts,
    default_package_media_dir,
    record_rtsp_clip,
)
from knock.integrations.sip_caller import SipCaller
from knock.providers.llm.ollama import OllamaProvider
from knock.providers.stt.base import STTProvider
from knock.providers.stt.whisper import WhisperSTTProvider
from knock.providers.tts.base import SynthesizedAudio, TTSProvider
from knock.providers.tts.kokoro import KokoroTTSProvider
from knock.providers.vision.base import VisionProvider, observe_scene, read_package_label
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

# Played right after listening ends and before the (sometimes slow --
# vision description, an LLM fallback call) processing that decides the
# actual reply, so the visitor gets some acknowledgement instead of dead
# air while that runs. A real sound file rather than a spoken placeholder
# phrase -- no TTS round-trip needed just to say "one moment," and no
# particular wording to get wrong. Both this and the error sound below are
# bundled under knock/sounds/ (see `_load_bundled_wav`).
_THINKING_SOUND = "thinking.wav"
# Played when speech capture itself fails outright (not the much more
# common "no reply" case of a visitor just going quiet) -- some audio
# feedback that something went wrong, instead of dead air.
_ERROR_SOUND = "error.wav"

# Spoken back to the visitor once someone at the Home Assistant end taps an
# action button on a delivery/appointment notification (see
# `handle_notification_action`) -- the visitor is presumably still standing
# at the door waiting to hear whether to stick around.
_ON_MY_WAY_PHRASE = "Good news, the homeowner says they're on their way."
_TURN_AWAY_PHRASE = "I'm sorry, but the homeowner isn't able to accept this right now."
_COMING_TO_DOOR_PHRASE = "Good news, they'll be right at the door."

# Upper bound on how long a reply waits for the scene capture (snapshot +
# vision) that was started in parallel with the greeting/listen window. In
# the common case it finished long ago; this only matters when the vision
# server is slow or down, and then the reply goes out without it rather
# than leaving the visitor standing there.
_SCENE_WAIT_SECONDS = 15.0


def _merge_observations(main: SceneObservation, extra: SceneObservation) -> SceneObservation:
    """Fold the package camera's view into the main camera's observation:
    the package lens is what actually sees a box on the porch, the main
    lens is what sees the person, so each contributes what it's good at.
    """
    carrying = list(dict.fromkeys([*main.carrying, *extra.carrying]))
    package_visible = (
        True
        if main.package_visible or extra.package_visible
        else (main.package_visible if main.package_visible is not None else extra.package_visible)
    )
    return main.model_copy(
        update={
            "carrying": carrying,
            "package_visible": package_visible,
            "package_label": extra.package_label,
        }
    )


def _default_session_id(device_id: str) -> str:
    safe = _UNSAFE_SESSION_CHARS.sub("_", device_id) or "unknown"
    return f"unifi-{safe}"[:128]


def _rms(pcm: bytes) -> float:
    """Root-mean-square amplitude of 16-bit mono PCM -- a cheap,
    dependency-free loudness measure used to skip transcribing near-silent
    audio (see `UnifiConfig.silence_rms_threshold`). Measured against a
    real doorbell mic: ambient room noise alone came back around RMS 5-6,
    nowhere near speech levels, so there's a wide, safe margin between
    "silence" and "someone is talking."
    """
    sample_count = len(pcm) // 2
    if sample_count == 0:
        return 0.0
    samples = array.array("h")
    samples.frombytes(pcm[: sample_count * 2])
    return (sum(s * s for s in samples) / sample_count) ** 0.5


@cache
def _load_bundled_wav(filename: str) -> SynthesizedAudio:
    """Read a WAV file bundled under `knock/sounds/` into a `SynthesizedAudio`.

    Cached since the same couple of files get played repeatedly over a
    process's lifetime and their content never changes -- cheap either
    way (these are a few hundred KB each), but no reason to re-read and
    re-decode from disk every single play.
    """
    wav_bytes = resources.files("knock").joinpath("sounds", filename).read_bytes()
    with wave.open(io.BytesIO(wav_bytes), "rb") as wav_file:
        return SynthesizedAudio(
            audio=wav_file.readframes(wav_file.getnframes()),
            rate=wav_file.getframerate(),
            width=wav_file.getsampwidth(),
            channels=wav_file.getnchannels(),
        )


def _write_wav(path: Path, audio: SynthesizedAudio) -> None:
    """Wrap raw PCM in a WAV header so PyAV (via `TalkbackStream`) can read it."""
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(audio.channels)
        wav_file.setsampwidth(audio.width)
        wav_file.setframerate(audio.rate)
        wav_file.writeframes(audio.audio)


# How much loud audio counts as "the visitor has started talking" -- so a
# single short noise (a car door, the tail of the chime) doesn't arm
# end-of-speech detection and cut the listen window short before the
# visitor has said anything.
_MIN_SPEECH_SECONDS = 0.25


class _EndOfSpeechDetector:
    """Decides when a visitor has finished talking: at least
    `_MIN_SPEECH_SECONDS` of audio louder than `threshold` (speech),
    followed by `end_silence_seconds` of continuous audio below it. Counts
    audio time from the samples themselves rather than wall-clock time, so
    a bursty RTSP delivery can't cut a pause short.
    """

    def __init__(self, sample_rate: int, threshold: float, end_silence_seconds: float) -> None:
        self._sample_rate = sample_rate
        self._threshold = threshold
        self._end_silence_seconds = end_silence_seconds
        self._speech_seconds = 0.0
        self._trailing_silence = 0.0

    def feed(self, pcm: bytes) -> bool:
        """Feed one chunk of 16-bit mono PCM; True once speech has ended."""
        if self._end_silence_seconds <= 0 or not pcm:
            return False
        chunk_seconds = (len(pcm) // 2) / self._sample_rate
        if _rms(pcm) >= self._threshold:
            self._speech_seconds += chunk_seconds
            self._trailing_silence = 0.0
        else:
            self._trailing_silence += chunk_seconds
        heard_speech = self._speech_seconds >= _MIN_SPEECH_SECONDS
        return heard_speech and self._trailing_silence >= self._end_silence_seconds


def _capture_rtsp_audio(
    rtsp_url: str,
    duration: float,
    sample_rate: int,
    verify_ssl: bool = True,
    *,
    end_silence_seconds: float = 0.0,
    silence_rms_threshold: float = 60.0,
) -> bytes:
    """Blocking: decode up to `duration` seconds of mono 16-bit PCM from an
    RTSP(S) stream's audio track -- or less, once `_EndOfSpeechDetector`
    decides the visitor has finished talking (`end_silence_seconds` > 0).

    Runs in a worker thread (via `asyncio.to_thread`) since PyAV's decode
    loop is itself blocking -- same reasoning `uiprotect`'s own
    `TalkbackStream` uses for the outbound direction.
    """
    resampler = av.AudioResampler(format="s16", layout="mono", rate=sample_rate)
    chunks: list[bytes] = []
    start = time.monotonic()
    end_of_speech = _EndOfSpeechDetector(sample_rate, silence_rms_threshold, end_silence_seconds)

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
            speech_ended = False
            for resampled in resampler.resample(frame):
                pcm = bytes(resampled.planes[0])
                chunks.append(pcm)
                speech_ended = end_of_speech.feed(pcm) or speech_ended
            if speech_ended or time.monotonic() - start >= duration:
                break

    return b"".join(chunks)


class _ProtectEventLike(Protocol):
    """The subset of `uiprotect.ProtectEvent` this bridge actually uses.

    Lets tests construct a plain stand-in instead of a real `ProtectEvent`
    (whose pydantic model construction has internal library requirements
    well beyond what KNOCK's own logic touches).
    """

    @property
    def id(self) -> str: ...

    @property
    def type(self) -> Any: ...

    @property
    def device_id(self) -> str: ...

    @property
    def smart_detect_types(self) -> Sequence[Any]: ...


@dataclass(frozen=True)
class _KnockEvent:
    """Synthetic `_ProtectEventLike` for a knock detected by an external
    (non-Protect) sensor -- e.g. a vibration sensor bridged through Home
    Assistant (see `HomeAssistantKnockListener`). Satisfies the same
    Protocol `UnifiBridge._run_conversation` already accepts from a real
    Protect websocket event, so a knock gets the exact same greeting/
    listen/speak flow a ring does, not a separate, thinner code path.

    `type` is deliberately not "ring": `_run_conversation` only waits out
    `ring_chime_delay_seconds` for an actual "ring" (there's no physical
    chime to wait out here), and `build_event` renders it as "Doorbell
    knock on <device_id>" in logs/audit -- distinguishable from a real
    button press rather than indistinguishable from one.
    """

    device_id: str
    type: str = "knock"
    smart_detect_types: Sequence[Any] = ()
    id: str = field(default_factory=lambda: f"knock-{uuid.uuid4().hex[:12]}")


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
        rtsp_audio_capture: Callable[[str, float, int, bool], bytes] | None = None,
        ha_notifier: HomeAssistantNotifier | None = None,
        snapshot_store: SnapshotStore | None = None,
        public_base_url: str | None = None,
        package_log: PackageLog | None = None,
        package_media_dir: Path | None = None,
        clip_recorder: Callable[[str, Path, float, bool], None] | None = None,
        sip_caller: SipCaller | None = None,
        doors: DoorsConfig | None = None,
    ) -> None:
        self.config = config or UnifiConfig()
        self.orchestrator = orchestrator or Orchestrator()
        self.session_store = session_store or JSONFileSessionStore()
        self.audit_log = audit_log or NullAuditLog()
        self.vision_provider = vision_provider
        self.tts_provider = tts_provider
        self.stt_provider = stt_provider
        self.client = client or ProtectApiClient(
            host=self.config.host,
            port=self.config.port,
            api_key=self.config.api_key.get_secret_value(),
            verify_ssl=self.config.verify_ssl,
            # uiprotect rejects half a login, so only pass a complete one.
            username=self.config.username if self.has_private_api else None,
            password=self.config.password.get_secret_value() if self.has_private_api else None,
        )
        self._talkback_stream_factory = talkback_stream_factory
        self._rtsp_audio_capture = rtsp_audio_capture or functools.partial(
            _capture_rtsp_audio,
            end_silence_seconds=self.config.end_silence_seconds,
            silence_rms_threshold=self.config.silence_rms_threshold,
        )
        self.package_log = package_log or NullPackageLog()
        # Where photos/clips of a package taken without a "Got it" are kept.
        # None (the default) keeps nothing on disk -- the notification still
        # carries the photo.
        self.package_media_dir = package_media_dir
        self._clip_recorder = clip_recorder or record_rtsp_clip
        self.packages = PackageAlerts(self)
        # Which door a camera belongs to. With no doors configured every
        # camera is its own implicit door (see core/doors.py), which is
        # exactly how the bridge always treated a camera.
        self.doors: DoorRegistry = resolve_doors(
            self.config, HomeAssistantConfig(), doors or DoorsConfig()
        )
        # Who gets told what (push notifications, phone calls) lives in
        # integrations/household.py; this bridge only supplies snapshots.
        # The lambdas look `self.client` / `self.orchestrator` up on each
        # call so replacing either after construction keeps working.
        self.household = HouseholdNotifier(
            fetch_snapshot=lambda device_id, package: self.client.get_public_api_camera_snapshot(
                device_id, package=package
            ),
            extract_details=lambda *args, **kwargs: self.orchestrator.extract_notification_details(
                *args, **kwargs
            ),
            ha_notifier=ha_notifier,
            sip_caller=sip_caller,
            tts_provider=tts_provider,
            snapshot_store=snapshot_store,
            public_base_url=public_base_url,
        )

    @property
    def has_private_api(self) -> bool:
        """A full local login is configured, on top of the API key -- what
        Protect's private API (e.g. video export) needs.
        """
        return bool(self.config.username and self.config.password.get_secret_value())

    # -- pure logic: directly testable without a real console -----------------

    def should_trigger(self, event: _ProtectEventLike) -> bool:
        if self.doors.for_protect_event(event.device_id) is None:
            return False

        event_type = str(event.type)
        if event_type in self.config.trigger_on:
            return True
        if event_type in _SMART_DETECT_EVENT_TYPES:
            smart_types = {str(t) for t in event.smart_detect_types}
            if self.packages.is_package_detection(event) and smart_types == {"package"}:
                # A box alone is handled by `PackageAlerts` -- never a
                # conversation, even with "package" in trigger_on.
                return False
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
        can't just persist forever, and `UnifiConfig.ring_cooldown_seconds`'
        for why a `ring` doesn't unconditionally force a restart -- an
        impatient double-press of the button would otherwise re-play the
        entire greeting mid-conversation instead of being debounced.
        """
        existing = self.session_store.load(session_id)
        is_ring = str(event.type) == "ring"
        is_stale = (
            existing is not None and (timestamp - existing.updated_at) > _SESSION_IDLE_TIMEOUT
        )
        is_within_cooldown = existing is not None and (
            timestamp - existing.updated_at
        ) <= timedelta(seconds=self.config.ring_cooldown_seconds)

        if existing is None or is_stale or (is_ring and not is_within_cooldown):
            return SessionState(session_id=session_id, updated_at=timestamp)
        return existing

    # -- household notifications (see integrations/household.py) -------------------

    @property
    def ha_notifier(self) -> HomeAssistantNotifier | None:
        return self.household.ha_notifier

    @ha_notifier.setter
    def ha_notifier(self, value: HomeAssistantNotifier | None) -> None:
        self.household.ha_notifier = value

    @property
    def public_base_url(self) -> str | None:
        return self.household.public_base_url

    @public_base_url.setter
    def public_base_url(self, value: str | None) -> None:
        self.household.public_base_url = value

    @property
    def snapshot_store(self) -> SnapshotStore:
        return self.household.snapshot_store

    @property
    def _call_tasks(self) -> set[asyncio.Task[None]]:
        return self.household._call_tasks

    async def _notify_household(
        self,
        device_id: str,
        message: str,
        *,
        actions: list[dict[str, str]] | None = None,
        category: NotificationCategory = "fyi",
        image_url: str | None = None,
        tag: str | None = None,
        door: Door | None = None,
    ) -> None:
        await self.household._notify_household(
            device_id,
            message,
            actions=actions,
            category=category,
            image_url=image_url,
            tag=tag,
            door=door,
        )

    async def _build_snapshot_url(self, device_id: str, *, package: bool = False) -> str | None:
        return await self.household._build_snapshot_url(device_id, package=package)

    def _snapshot_url_for(self, snapshot: bytes) -> str | None:
        return self.household._snapshot_url_for(snapshot)

    async def _on_escalate_notify(
        self,
        decision: ResponseDecision,
        visitor_event: VisitorEvent,
        device_id: str,
        door: Door | None = None,
    ) -> None:
        await self.household._on_escalate_notify(decision, visitor_event, device_id, door)

    async def _handle_turn_notifications(
        self,
        decision: ResponseDecision,
        visitor_event: VisitorEvent,
        device_id: str,
        door: Door | None = None,
    ) -> None:
        await self.household._handle_turn_notifications(decision, visitor_event, device_id, door)

    async def _run_systems_check_and_reply(self, device_id: str, door: Door | None = None) -> None:
        """Voice-triggered diagnostic (see core/systems_check.py) -- tests
        whatever's actually configured on this bridge and speaks back a
        short result. Called by handle_event before orchestrator.respond()
        is ever reached, so this never touches PolicyEngine/classification.
        """
        vision_provider = self.vision_provider
        ha_notifier = self.ha_notifier
        look_device = door.look_devices[0] if door is not None else device_id
        speak_device = door.speak_device if door is not None else device_id

        snapshot_fetcher: Callable[[], Awaitable[bytes | None]] | None = None
        if vision_provider is not None:

            async def snapshot_fetcher() -> bytes | None:
                return await self.client.get_public_api_camera_snapshot(look_device)

        notify: Callable[[str], None] | None = None
        if ha_notifier is not None:

            def notify(message: str) -> None:
                ha_notifier.notify(message, category="fyi")

        results = await run_systems_check(
            llm_provider=self.orchestrator.llm_provider,
            vision_provider=vision_provider,
            snapshot_fetcher=snapshot_fetcher,
            notify=notify,
        )
        summary = summarize_systems_check(results)
        logger.info("Systems check for %s: %s", device_id, summary)
        if self.tts_provider is not None:
            await self.speak_to_visitor(speak_device, summary)

    def _detector_scene(self, event: _ProtectEventLike) -> SceneContext:
        """The scene context UniFi's own detector gives for free -- smart
        detect types (person/package/vehicle) -- with no vision model
        involved at all.
        """
        return SceneContext(
            camera=event.device_id,
            detected_labels=[str(t) for t in event.smart_detect_types],
            captured_at=datetime.now(UTC),
        )

    async def _capture_scene(
        self, event: _ProtectEventLike, door: Door | None = None
    ) -> tuple[SceneContext, bytes | None]:
        """Snapshot + structured vision observation for this event's camera,
        plus the raw snapshot (for the opt-in send-image-to-brain path).

        Best-effort like every other vision path here: no snapshot, or any
        snapshot/vision failure, still returns the detector-only scene.
        When UniFi's own detector already saw a package, the doorbell's
        package camera is looked through too and merged in.
        """
        scene = self._detector_scene(event)
        if self.vision_provider is None:
            return scene, None
        vision_provider = self.vision_provider
        # The door's main snapshot camera (the triggering camera, for an
        # implicit door).
        look_device = door.look_devices[0] if door is not None else event.device_id
        try:
            snapshot = await self.client.get_public_api_camera_snapshot(look_device)
            if snapshot is None:
                return scene, None
            observation = await asyncio.to_thread(observe_scene, vision_provider, snapshot)
            if "package" in scene.detected_labels:
                try:
                    package_snapshot = await self.client.get_public_api_camera_snapshot(
                        look_device, package=True
                    )
                    if package_snapshot is not None:
                        package_observation = await asyncio.to_thread(
                            observe_scene, vision_provider, package_snapshot
                        )
                        label = await asyncio.to_thread(
                            read_package_label, vision_provider, package_snapshot
                        )
                        if label is not None:
                            package_observation = package_observation.model_copy(
                                update={"package_label": label}
                            )
                        observation = _merge_observations(observation, package_observation)
                except Exception as exc:  # noqa: BLE001 - package view is a bonus
                    logger.info(
                        "Package-camera observation failed for %s: %s", event.device_id, exc
                    )
            return scene.model_copy(update={"observation": observation}), snapshot
        except Exception as exc:  # noqa: BLE001 - vision enrichment is best-effort
            logger.warning("Vision enrichment failed for device %s: %s", event.device_id, exc)
            return scene, None

    async def handle_event(self, event: _ProtectEventLike) -> ResponseDecision | None:
        is_package = self.packages.is_package_detection(event)
        converses = self.should_trigger(event)
        if not is_package and not converses:
            return None

        door = self.doors.for_camera(event.device_id)
        # Package state is per physical camera: the camera the door looks
        # through (for an implicit door, the triggering camera -- unchanged).
        presence_key = door.look_devices[0]

        # Counted before the package check so a box seen mid-conversation is
        # held until the visitor leaves, not announced while they stand there.
        if converses:
            self.packages.conversation_started(presence_key)
        try:
            if is_package:
                await self.packages.on_package_detected(event.device_id)
            if not converses:
                return None
            return await self._run_conversation(event, door)
        finally:
            if converses:
                self.packages.conversation_ended(presence_key)

    async def handle_knock(self, device_id: str) -> ResponseDecision | None:
        """Entry point for an external (non-Protect) knock-sensor trigger
        -- see `HomeAssistantKnockListener`. Skips `should_trigger`'s
        camera-id/trigger_on filtering and the package-detection branch
        entirely: a knock listener is already configured for exactly one
        specific camera, so there's no ambiguity to filter, and a knock is
        never a package event. Still coordinates with `PackageAlerts` the
        same way a real conversation does, so a package notification
        doesn't fire mid-visit.
        """
        event = _KnockEvent(device_id=device_id)
        door = self.doors.for_camera(device_id)
        presence_key = door.look_devices[0]
        self.packages.conversation_started(presence_key)
        try:
            return await self._run_conversation(event, door)
        finally:
            self.packages.conversation_ended(presence_key)

    def _systems_check_interceptor(
        self, device_id: str, door: Door | None = None
    ) -> Callable[[str], Awaitable[bool]]:
        async def intercept(transcript: str) -> bool:
            if not is_systems_check_trigger(transcript):
                return False
            await self._run_systems_check_and_reply(device_id, door)
            return True

        return intercept

    async def _run_conversation(
        self, event: _ProtectEventLike, door: Door | None = None
    ) -> ResponseDecision | None:
        if door is None:
            door = self.doors.for_camera(event.device_id)
        if str(event.type) == "ring" and self.config.ring_chime_delay_seconds > 0:
            # Let the physical doorbell chime finish before KNOCK starts
            # talking over it or opens its listen window early enough to
            # capture the tail of the chime instead of just the visitor. A
            # silent smart-detect trigger has no chime to wait out.
            await asyncio.sleep(self.config.ring_chime_delay_seconds)

        visitor_event = self.build_event(event)
        state = self._start_or_resume_session(door.session_id, event, visitor_event.timestamp)

        # Every role comes from the door. For an implicit door that is the
        # triggering camera for all of them, as it always was.
        listen_device = door.listen_device
        speak_device = door.speak_device
        look_device = door.look_devices[0]
        hooks = ConversationHooks(
            listen=(lambda: self.listen_to_visitor(listen_device)) if self.stt_provider else None,
            speak=(
                (lambda text: self.speak_to_visitor(speak_device, text))
                if self.tts_provider
                else None
            ),
            play_thinking_tone=(
                (lambda: self._play_thinking_tone(speak_device)) if self.tts_provider else None
            ),
            capture_scene=lambda: self._capture_scene(event, door),
            scene_fallback=lambda: self._detector_scene(event),
            intercept_transcript=self._systems_check_interceptor(look_device, door),
            before_speak=lambda d, ve: self._on_escalate_notify(d, ve, look_device, door),
            after_speak=lambda d, ve: self._handle_turn_notifications(d, ve, look_device, door),
        )

        return await run_conversation_loop(
            visitor_event,
            state=state,
            orchestrator=self.orchestrator,
            session_store=self.session_store,
            hooks=hooks,
            audit_log=self.audit_log,
            max_turns=_MAX_CONVERSATION_TURNS,
            scene_wait_seconds=_SCENE_WAIT_SECONDS,
        )

    async def handle_notification_action(self, action_id: str, device_id: str) -> None:
        """React to a Home Assistant mobile app notification-action tap --
        the "I'm on my way" / "Turn them away" buttons on a
        signature-required delivery or food-delivery notification, or the
        single "I'm coming to the door" button on a service_appointment
        notification (see `handle_event`).

        Called by `HomeAssistantActionListener` as its `on_action` callback
        (see `main()`), which has already parsed the device id back out of
        the action identifier itself.
        """
        if action_id == KNOCK_ON_MY_WAY_ACTION:
            await self.speak_to_visitor(device_id, _ON_MY_WAY_PHRASE)
        elif action_id == KNOCK_TURN_AWAY_ACTION:
            await self.speak_to_visitor(device_id, _TURN_AWAY_PHRASE)
        elif action_id == KNOCK_COMING_TO_DOOR_ACTION:
            await self.speak_to_visitor(device_id, _COMING_TO_DOOR_PHRASE)
        elif action_id == KNOCK_PACKAGE_ACK_ACTION:
            # Nothing spoken -- the visitor who left it is long gone.
            await self.packages.acknowledge(device_id)
        else:
            logger.warning(
                "Unrecognized notification action %r for device %s", action_id, device_id
            )

    def _rtsp_url(self, device_id: str) -> str | None:
        camera = self.client.public_bootstrap.cameras.get(device_id)
        streams = camera.rtsps_streams if camera is not None else None
        url = streams.get_stream_url(self.config.rtsp_quality) if streams else None
        return str(url) if url else None

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
            url = self._rtsp_url(device_id)
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

            if _rms(pcm) < self.config.silence_rms_threshold:
                # Whisper (and STT models generally) will still confidently
                # hallucinate *something* from near-silent audio rather than
                # admit "nothing was said" -- that hallucinated non-empty
                # text is exactly what let the conversation loop in
                # handle_event() run for its full turn cap instead of
                # recognizing the visitor had already left. Checking loudness
                # before ever asking the model avoids the question entirely.
                logger.info("Captured audio for %s is near-silent; skipping STT", device_id)
                return ""

            transcript = await self.stt_provider.transcribe(
                pcm, rate=_CAPTURE_SAMPLE_RATE, width=2, channels=1
            )
            logger.info("Transcribed for %s: %r", device_id, transcript)
            return transcript
        except Exception as exc:  # noqa: BLE001 - speech capture is best-effort
            logger.warning("Speech capture failed for device %s: %s", device_id, exc)
            await self._play_error_sound(device_id)
            return ""

    async def speak_to_visitor(self, device_id: str, text: str) -> None:
        """Synthesize `text` and stream it to `device_id`'s speaker, best-effort.

        Any failure here (no speaker on this camera, synthesis error,
        streaming error) is logged and swallowed -- talkback is an
        enhancement on top of the text response, never a requirement for it.

        Empty `text` is a deliberate silence (see Orchestrator.respond()'s
        "not_addressed" case -- captured audio that wasn't actually directed
        at the door), not a synthesis request; skip rather than ask the TTS
        provider to voice nothing.
        """
        if self.tts_provider is None or not text:
            return

        try:
            audio = await self.tts_provider.synthesize(text)
        except Exception as exc:  # noqa: BLE001 - talkback is best-effort
            logger.warning("Talkback to device %s failed: %s", device_id, exc)
            return
        await self._play_audio(device_id, audio)

    async def _play_thinking_tone(self, device_id: str) -> None:
        """A short audio cue played while KNOCK is processing a reply, in
        place of a spoken placeholder phrase -- no TTS round-trip needed
        just to say "one moment," and nothing tying it to any particular
        wording.
        """
        await self._play_audio(device_id, _load_bundled_wav(_THINKING_SOUND))

    async def _play_error_sound(self, device_id: str) -> None:
        """Played when speech capture itself fails outright -- some audio
        feedback that something went wrong, instead of dead air.
        """
        await self._play_audio(device_id, _load_bundled_wav(_ERROR_SOUND))

    async def _play_audio(self, device_id: str, audio: SynthesizedAudio) -> None:
        """Stream already-synthesized PCM audio to `device_id`'s speaker,
        best-effort -- shared by `speak_to_visitor` (TTS output) and the
        bundled-sound playback above (thinking/error cues, no TTS involved).

        Any failure here (no speaker on this camera, streaming error) is
        logged and swallowed -- talkback is an enhancement, never a
        requirement for the rest of the response.
        """
        try:
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
        # uiprotect swallows (and so silently loses) anything this raises.
        try:
            self.packages.track_presence(event, change)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Presence tracking failed for %s: %s", event.device_id, exc)
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
        # Only worth its own connection when at least one sensor->camera
        # mapping is actually configured -- see
        # HomeAssistantConfig.knock_sensors's docstring.
        if ha_config.knock_sensors:
            knock_listener = HomeAssistantKnockListener(
                config=ha_config, on_knock=bridge.handle_knock
            )
            tasks.append(knock_listener.run())
    await asyncio.gather(*tasks)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    ollama_config = OllamaConfig.from_env()
    vision_config = VisionConfig.from_env()
    # None (unset) leaves this whole layer off -- the deterministic
    # apply_style() backstop still applies regardless. When set, this is a
    # second OllamaProvider pointed at a different model (and optionally a
    # different Ollama server, see OllamaConfig.safety_check_config) so a
    # bigger/more careful model can be used for this one extra-opinion call
    # without slowing down every-turn response phrasing.
    safety_check_config = ollama_config.safety_check_config()
    safety_check_provider = (
        OllamaProvider(config=safety_check_config) if safety_check_config is not None else None
    )
    llm_provider = OllamaProvider(config=ollama_config)
    vision_provider = OllamaVisionProvider(config=vision_config)
    # Load every model now rather than on the first ring -- see warm_up().
    for provider in (llm_provider, vision_provider, safety_check_provider):
        if provider is not None:
            provider.warm_up()
    orchestrator = Orchestrator(
        llm_provider=llm_provider,
        use_tool_calling=resolve_tool_calling(ollama_config),
        safety_check_provider=safety_check_provider,
        send_image_to_brain=vision_config.send_image_to_brain,
    )
    ha_config = HomeAssistantConfig.from_env()
    knock_config = KnockConfig.from_env()
    bridge = UnifiBridge(
        config=UnifiConfig.from_env(),
        orchestrator=orchestrator,
        vision_provider=vision_provider,
        stt_provider=WhisperSTTProvider(config=WhisperConfig.from_env()),
        tts_provider=KokoroTTSProvider(config=KokoroConfig.from_env()),
        # A no-op if KNOCK_HA_NOTIFY_SERVICE isn't set -- no need to check
        # whether Home Assistant is actually configured before wiring it in.
        ha_notifier=HomeAssistantNotifier(config=ha_config),
        # Without this, the bridge falls back to its class default
        # (NullAuditLog) and every real conversation silently never reaches
        # audit.jsonl / the web UI's History page -- the CLI and web API
        # already construct a real JSONLAuditLog() the same way.
        audit_log=JSONLAuditLog(),
        package_log=JSONLPackageLog(),
        package_media_dir=default_package_media_dir(),
        sip_caller=SipCaller(SipConfig.from_env()),
        public_base_url=knock_config.public_base_url,
        doors=DoorsConfig.from_env(),
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
