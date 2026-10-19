"""Announcement phone calls over SIP -- SIP roadmap Stage 1.

For the notification categories in `SipConfig.call_on`, KNOCK also rings a
phone and speaks the notification's summary, then hangs up: picking up a
desk phone (a UniFi Talk handset, an analog phone behind an ATA, anything
on the SIP server) becomes an alternative to the push notification. Not a
replacement -- the push still goes out first, and a call that fails or
goes unanswered changes nothing else.

Uses pyVoIP (GPL-3.0; see the README's license note). Its API is blocking
and thread-based, so `SipCaller.announce` runs in a worker thread
(`asyncio.to_thread`). pyVoIP 1.6 has no way to cancel a call that's still
ringing, so an unanswered target rings until the SIP server's own ring
timeout ends it; KNOCK just stops waiting after `ring_timeout_seconds` and
moves on to the next target.

Each announcement registers, calls and unregisters again rather than
keeping a registration open -- simplest correct shape for occasional
outbound calls. Answering *inbound* calls (Stage 4) will need a long-lived
registration instead.
"""

from __future__ import annotations

import array
import logging
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import av

from knock.config import SipConfig
from knock.providers.tts.base import SynthesizedAudio

logger = logging.getLogger(__name__)

# What pyVoIP's RTP layer expects from `write_audio`: 8 kHz mono, 8-bit
# unsigned linear PCM (it re-biases and mu-law/A-law encodes it itself).
SIP_SAMPLE_RATE = 8000

# A beat of silence after the callee picks up, so the first word isn't
# clipped while their handset's audio path opens.
_ANSWER_LEAD_IN_SECONDS = 0.6
# How often call state is polled while ringing.
_POLL_SECONDS = 0.1


def to_sip_audio(audio: SynthesizedAudio) -> bytes:
    """Convert TTS output to what pyVoIP plays: 8 kHz mono unsigned 8-bit."""
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
    # Signed 16-bit -> unsigned 8-bit: keep the top byte, shift to center on 128.
    return bytes((sample >> 8) + 128 for sample in pcm16)


def detect_local_ip(server: str, port: int) -> str:
    """The local address the OS would use to reach `server` -- what the
    SIP server should send call audio back to. A UDP `connect` sends
    nothing; it only asks the routing table.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect((server, port))
        return str(probe.getsockname()[0])


class _CallLike(Protocol):
    @property
    def state(self) -> Any: ...

    def write_audio(self, data: bytes) -> None: ...

    def hangup(self) -> None: ...


class _PhoneLike(Protocol):
    def start(self) -> None: ...

    def stop(self) -> None: ...

    def call(self, number: str) -> _CallLike: ...


PhoneFactory = Callable[[SipConfig, str], _PhoneLike]


def _pyvoip_phone(config: SipConfig, local_ip: str) -> _PhoneLike:
    # Imported here so a deployment that never enables SIP never loads it.
    from pyVoIP.VoIP import VoIPPhone

    phone: _PhoneLike = VoIPPhone(
        config.server,
        config.port,
        config.username,
        config.password.get_secret_value(),
        myIP=local_ip,
        sipPort=config.local_sip_port,
        rtpPortLow=config.rtp_port_low,
        rtpPortHigh=config.rtp_port_high,
    )
    return phone


def _state_name(call: _CallLike) -> str:
    state = call.state
    return str(getattr(state, "value", state))


@dataclass
class CallOutcome:
    answered_by: str | None = None
    attempted: list[str] = field(default_factory=list)
    error: str | None = None


class SipCaller:
    def __init__(
        self,
        config: SipConfig,
        phone_factory: PhoneFactory | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self._phone_factory = phone_factory or _pyvoip_phone
        self._sleep = sleep
        # One call at a time: a second announcement while one is ringing
        # would just fight it for the same phones.
        self._busy = threading.Lock()

    @property
    def configured(self) -> bool:
        return bool(
            self.config.enabled
            and self.config.server
            and self.config.username
            and self.config.call_targets
        )

    def announce(self, audio: bytes) -> CallOutcome:
        """Blocking: ring each target in order until one answers, play
        `audio` (see `to_sip_audio`) to it, and hang up.
        """
        outcome = CallOutcome()
        if not self.configured:
            outcome.error = "SIP calling isn't configured"
            return outcome
        if not self._busy.acquire(blocking=False):
            outcome.error = "another call is already in progress"
            return outcome
        try:
            local_ip = self.config.local_ip or detect_local_ip(self.config.server, self.config.port)
            phone = self._phone_factory(self.config, local_ip)
            phone.start()
        except Exception as exc:  # noqa: BLE001 - a call is best-effort
            self._busy.release()
            outcome.error = f"couldn't register with the SIP server: {exc}"
            return outcome
        try:
            for target in self.config.call_targets:
                outcome.attempted.append(target)
                if self._ring_and_play(phone, target, audio):
                    outcome.answered_by = target
                    break
        except Exception as exc:  # noqa: BLE001
            outcome.error = str(exc)
        finally:
            try:
                phone.stop()
            except Exception as exc:  # noqa: BLE001
                logger.info("SIP unregister failed: %s", exc)
            self._busy.release()
        return outcome

    def _ring_and_play(self, phone: _PhoneLike, target: str, audio: bytes) -> bool:
        call = phone.call(target)
        waited = 0.0
        while _state_name(call) == "DIALING" and waited < self.config.ring_timeout_seconds:
            self._sleep(_POLL_SECONDS)
            waited += _POLL_SECONDS
        state = _state_name(call)
        if state != "ANSWERED":
            logger.info("SIP call to %s not answered (%s)", target, state)
            return False

        lead_in = bytes([128]) * int(SIP_SAMPLE_RATE * _ANSWER_LEAD_IN_SECONDS)
        call.write_audio(lead_in + audio)
        # write_audio only queues; RTP sends it in real time, so wait it out.
        self._sleep((len(lead_in) + len(audio)) / SIP_SAMPLE_RATE + 0.5)
        if _state_name(call) == "ANSWERED":
            try:
                call.hangup()
            except Exception as exc:  # noqa: BLE001 - they may hang up first
                logger.info("SIP hangup to %s failed: %s", target, exc)
        return True
