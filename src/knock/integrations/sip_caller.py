"""Announcement phone calls over SIP -- SIP roadmap Stage 1.

For the notification categories in `SipConfig.call_on`, KNOCK also rings a
phone and speaks the notification's summary, then hangs up: picking up a
desk phone (a UniFi Talk handset, an analog phone behind an ATA, anything
on the SIP server) becomes an alternative to the push notification. Not a
replacement -- the push still goes out first, and a call that fails or
goes unanswered changes nothing else.

The SIP side is baresip, run as a helper process and driven over its
control port (see `integrations/baresip.py`). It's started on the first
call, not at bridge startup, so a deployment that never enables SIP never
runs it -- and once started it stays registered. A call works like this:
write the announcement WAV, `dial`, wait for `CALL_ESTABLISHED`, and let
baresip hang up by itself when the WAV runs out. A target that doesn't
answer within `ring_timeout_seconds` gets a real `hangup` (a SIP CANCEL, so
the phone stops ringing) and the next target is tried.

`SipCaller.announce` blocks, so callers run it in a worker thread
(`asyncio.to_thread`).
"""

from __future__ import annotations

import array
import atexit
import logging
import threading
import time
import wave
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import av

from knock.config import SipConfig
from knock.integrations.baresip import BaresipProcess
from knock.providers.tts.base import SynthesizedAudio

logger = logging.getLogger(__name__)

# Phone audio (G.711): 8 kHz mono. The announcement WAV is written as 16-bit
# PCM at this rate so baresip never has to resample it.
SIP_SAMPLE_RATE = 8000

# A beat of silence after the callee picks up, so the first word isn't
# clipped while their handset's audio path opens.
_ANSWER_LEAD_IN_SECONDS = 0.6
# How long a freshly started baresip gets to register with the SIP server.
_REGISTER_TIMEOUT_SECONDS = 10.0
# After the announcement should have finished, how long to wait for baresip's
# own end-of-file hangup before hanging up explicitly.
_PLAYBACK_GRACE_SECONDS = 5.0
# How long to wait for CALL_CLOSED after asking baresip to hang up.
_HANGUP_WAIT_SECONDS = 3.0

_CALL_EVENTS = {"CALL_RINGING", "CALL_PROGRESS", "CALL_ESTABLISHED", "CALL_CLOSED"}


def to_sip_audio(audio: SynthesizedAudio) -> bytes:
    """Convert TTS output to 8 kHz mono signed 16-bit PCM."""
    fmt = {1: "u8", 2: "s16", 4: "s32"}[audio.width]
    layout = "mono" if audio.channels == 1 else "stereo"
    samples = len(audio.audio) // (audio.width * audio.channels)
    frame = av.AudioFrame(format=fmt, layout=layout, samples=samples)
    frame.planes[0].update(audio.audio[: samples * audio.width * audio.channels])
    frame.sample_rate = audio.rate
    resampler = av.AudioResampler(format="s16", layout="mono", rate=SIP_SAMPLE_RATE)
    pcm16 = array.array("h")
    for out in [*resampler.resample(frame), *resampler.resample(None)]:
        pcm16.frombytes(bytes(out.planes[0])[: out.samples * 2])
    return pcm16.tobytes()


def write_announcement(path: Path, pcm16: bytes) -> float:
    """Write `pcm16` (see `to_sip_audio`) as the WAV baresip plays, with a
    short silent lead-in. Returns its length in seconds.
    """
    lead_in = bytes(2 * int(SIP_SAMPLE_RATE * _ANSWER_LEAD_IN_SECONDS))
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SIP_SAMPLE_RATE)
        wav.writeframes(lead_in + pcm16)
    return (len(lead_in) + len(pcm16)) / 2 / SIP_SAMPLE_RATE


def target_uri(target: str, server: str, port: int = 5060) -> str:
    """An extension ("101") becomes `sip:101@<server>` (plus `:<port>` when
    it isn't the default 5060); a full address is kept as given.
    """
    if target.startswith("sip:"):
        return target
    if "@" in target:
        return f"sip:{target}"
    host = server if port == 5060 else f"{server}:{port}"
    return f"sip:{target}@{host}"


class _ControlLike(Protocol):
    def command(self, name: str, params: str = "", timeout: float = 5.0) -> dict[str, Any]: ...

    def next_event(self, timeout: float) -> dict[str, Any] | None: ...

    def close(self) -> None: ...


class _BackendLike(Protocol):
    @property
    def running(self) -> bool: ...

    @property
    def announcement_path(self) -> Path: ...

    def start(self) -> None: ...

    def connect(self) -> _ControlLike: ...

    def stop(self) -> None: ...


@dataclass
class CallOutcome:
    answered_by: str | None = None
    attempted: list[str] = field(default_factory=list)
    error: str | None = None


class SipCaller:
    def __init__(
        self,
        config: SipConfig,
        backend: _BackendLike | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self._backend: _BackendLike = backend or BaresipProcess(config)
        self._clock = clock
        # One call at a time: they share baresip and the announcement file.
        self._busy = threading.Lock()
        self._registered = False
        self._atexit_registered = False

    @property
    def configured(self) -> bool:
        return bool(
            self.config.enabled
            and self.config.server
            and self.config.username
            and self.config.call_targets
        )

    def close(self) -> None:
        self._backend.stop()
        self._registered = False

    def announce(self, audio: bytes, targets: list[str] | None = None) -> CallOutcome:
        """Blocking: ring each target in order until one answers, play
        `audio` (see `to_sip_audio`) to it, and let the call end.

        `targets` overrides `config.call_targets` for this call (a door's
        own phones); None rings the configured list, as always.
        """
        outcome = CallOutcome()
        if not self.configured:
            outcome.error = "SIP calling isn't configured"
            return outcome
        if not self._busy.acquire(blocking=False):
            outcome.error = "another call is already in progress"
            return outcome
        try:
            control = self._ready()
        except Exception as exc:  # noqa: BLE001 - a call is best-effort
            self._busy.release()
            outcome.error = str(exc)
            return outcome
        try:
            seconds = write_announcement(self._backend.announcement_path, audio)
            for target in targets or self.config.call_targets:
                outcome.attempted.append(target)
                if self._ring_and_play(control, target, seconds):
                    outcome.answered_by = target
                    break
        except Exception as exc:  # noqa: BLE001
            outcome.error = str(exc)
        finally:
            control.close()
            self._busy.release()
        return outcome

    def _ready(self) -> _ControlLike:
        """Start baresip if needed and make sure it's registered."""
        if not self._backend.running:
            self._registered = False
            self._backend.start()
            if not self._atexit_registered:
                atexit.register(self.close)
                self._atexit_registered = True
        control = self._backend.connect()
        if self._registered or not self.config.registration:
            return control
        deadline = self._clock() + _REGISTER_TIMEOUT_SECONDS
        while self._clock() < deadline:
            event = control.next_event(timeout=max(deadline - self._clock(), 0.0))
            if event is None:
                break
            if event.get("type") == "REGISTER_OK":
                self._registered = True
                return control
            if event.get("type") == "REGISTER_FAIL":
                control.close()
                self.close()  # restart (and re-read the login) next time
                raise RuntimeError(f"the SIP server rejected KNOCK's login: {event.get('param')}")
        control.close()
        self.close()
        raise TimeoutError("couldn't register with the SIP server in time")

    def _ring_and_play(self, control: _ControlLike, target: str, seconds: float) -> bool:
        uri = target_uri(target, self.config.server, self.config.port)
        user_part = uri.split("@", 1)[0]
        response = control.command("dial", uri)
        if not response.get("ok"):
            logger.info("SIP dial to %s refused: %s", target, response.get("data"))
            return False

        answered = False
        deadline = self._clock() + self.config.ring_timeout_seconds
        while True:
            remaining = deadline - self._clock()
            if remaining <= 0:
                break
            event = control.next_event(timeout=remaining)
            if event is None:
                break
            peer = str(event.get("peeruri") or "")
            if event.get("type") not in _CALL_EVENTS or (peer and not peer.startswith(user_part)):
                continue
            if event["type"] == "CALL_ESTABLISHED" and not answered:
                answered = True
                deadline = self._clock() + seconds + _PLAYBACK_GRACE_SECONDS
            elif event["type"] == "CALL_CLOSED":
                if not answered:
                    logger.info("SIP call to %s not answered: %s", target, event.get("param"))
                return answered

        # Still ringing past the timeout, or still connected well past the
        # end of the announcement: hang up (a CANCEL while ringing).
        if not answered:
            logger.info(
                "SIP call to %s not answered within %ss", target, self.config.ring_timeout_seconds
            )
        control.command("hangup")
        hangup_deadline = self._clock() + _HANGUP_WAIT_SECONDS
        while self._clock() < hangup_deadline:
            event = control.next_event(timeout=max(hangup_deadline - self._clock(), 0.0))
            if event is None or event.get("type") == "CALL_CLOSED":
                break
        return answered
