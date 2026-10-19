"""SIP announcement calls -- see `knock.integrations.sip_caller`."""

import array
import asyncio
import math
from types import SimpleNamespace
from unittest.mock import MagicMock

from knock.config import SipConfig, UnifiConfig
from knock.core.session_store import JSONFileSessionStore
from knock.integrations.sip_caller import SIP_SAMPLE_RATE, SipCaller, to_sip_audio
from knock.integrations.unifi import UnifiBridge
from knock.providers.tts.base import SynthesizedAudio


class _FakeCall:
    """A call that answers after `answer_after` polls (or never), and can be
    hung up by the far end once audio starts playing.
    """

    def __init__(self, answer_after: int | None, remote_hangs_up: bool = False) -> None:
        self._polls_left = answer_after
        self._remote_hangs_up = remote_hangs_up
        self._state = "DIALING"
        self.audio = b""
        self.hung_up = False

    @property
    def state(self) -> SimpleNamespace:
        if self._state == "DIALING" and self._polls_left is not None:
            if self._polls_left <= 0:
                self._state = "ANSWERED"
            self._polls_left -= 1
        return SimpleNamespace(value=self._state)

    def write_audio(self, data: bytes) -> None:
        self.audio += data
        if self._remote_hangs_up:
            self._state = "ENDED"

    def hangup(self) -> None:
        if self._state != "ANSWERED":
            raise RuntimeError("Call is not answered")
        self.hung_up = True
        self._state = "ENDED"


class _FakePhone:
    def __init__(self, calls: dict[str, _FakeCall], start_error: Exception | None = None):
        self._calls = calls
        self._start_error = start_error
        self.dialed: list[str] = []
        self.started = self.stopped = False

    def start(self) -> None:
        if self._start_error is not None:
            raise self._start_error
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def call(self, number: str) -> _FakeCall:
        self.dialed.append(number)
        return self._calls[number]


def _config(**overrides) -> SipConfig:
    defaults = {
        "enabled": True,
        "server": "192.0.2.10",
        "username": "knock",
        "password": "secret",
        "local_ip": "192.0.2.20",
        "call_targets": ["101"],
        "ring_timeout_seconds": 1.0,
    }
    return SipConfig(**{**defaults, **overrides})


def _caller(phone: _FakePhone, **config) -> tuple[SipCaller, list[float]]:
    slept: list[float] = []
    caller = SipCaller(_config(**config), phone_factory=lambda cfg, ip: phone, sleep=slept.append)
    return caller, slept


# -- SipCaller ------------------------------------------------------------------------


def test_an_answered_call_plays_the_audio_after_a_lead_in_then_hangs_up() -> None:
    call = _FakeCall(answer_after=2)
    phone = _FakePhone({"101": call})
    caller, slept = _caller(phone)

    outcome = caller.announce(b"\x90" * 800)

    assert outcome.answered_by == "101"
    assert outcome.error is None
    assert call.audio.endswith(b"\x90" * 800)
    lead_in = call.audio[:-800]
    assert lead_in and set(lead_in) == {128}  # silence before the first word
    assert call.hung_up
    assert phone.started and phone.stopped
    # Waited out real-time playback before hanging up.
    assert slept[-1] >= len(call.audio) / SIP_SAMPLE_RATE


def test_an_unanswered_target_times_out_and_the_next_one_is_tried() -> None:
    phone = _FakePhone({"101": _FakeCall(answer_after=None), "102": _FakeCall(answer_after=0)})
    caller, _ = _caller(phone, call_targets=["101", "102"])

    outcome = caller.announce(b"\x90")

    assert phone.dialed == ["101", "102"]
    assert outcome.attempted == ["101", "102"]
    assert outcome.answered_by == "102"


def test_once_someone_answers_nobody_else_is_called() -> None:
    phone = _FakePhone({"101": _FakeCall(answer_after=0), "102": _FakeCall(answer_after=0)})
    caller, _ = _caller(phone, call_targets=["101", "102"])

    outcome = caller.announce(b"\x90")

    assert phone.dialed == ["101"]
    assert outcome.answered_by == "101"


def test_nobody_answering_is_reported_not_raised() -> None:
    phone = _FakePhone({"101": _FakeCall(answer_after=None)})
    caller, _ = _caller(phone)

    outcome = caller.announce(b"\x90")

    assert outcome.answered_by is None
    assert outcome.attempted == ["101"]
    assert phone.stopped


def test_the_far_end_hanging_up_first_is_fine() -> None:
    call = _FakeCall(answer_after=0, remote_hangs_up=True)
    caller, _ = _caller(_FakePhone({"101": call}))

    outcome = caller.announce(b"\x90")

    assert outcome.answered_by == "101"
    assert not call.hung_up  # nothing left to hang up


def test_a_registration_failure_is_reported_and_releases_the_line() -> None:
    phone = _FakePhone({}, start_error=OSError("401 Unauthorized"))
    caller, _ = _caller(phone)

    first = caller.announce(b"\x90")
    second = caller.announce(b"\x90")

    assert first.error is not None and "register" in first.error
    # The busy lock was released, so the next attempt tries again.
    assert second.error is not None and "register" in second.error


def test_only_one_call_at_a_time() -> None:
    caller, _ = _caller(_FakePhone({"101": _FakeCall(answer_after=0)}))
    assert caller._busy.acquire()

    outcome = caller.announce(b"\x90")

    assert outcome.error == "another call is already in progress"


def test_unconfigured_caller_does_nothing() -> None:
    factory = MagicMock()
    caller = SipCaller(_config(call_targets=[]), phone_factory=factory)

    outcome = caller.announce(b"\x90")

    assert outcome.error == "SIP calling isn't configured"
    factory.assert_not_called()


# -- audio conversion -----------------------------------------------------------------


def test_tts_audio_becomes_8khz_unsigned_8bit() -> None:
    rate = 22050
    tone = array.array(
        "h", [int(12000 * math.sin(2 * math.pi * 440 * i / rate)) for i in range(rate)]
    )

    out = to_sip_audio(SynthesizedAudio(audio=tone.tobytes(), rate=rate, width=2, channels=1))

    assert abs(len(out) - SIP_SAMPLE_RATE) <= 10  # one second at 8 kHz
    assert 60 < min(out) < 128 < max(out) < 196  # centered on 128, right level


def test_silence_maps_to_the_unsigned_midpoint() -> None:
    out = to_sip_audio(SynthesizedAudio(audio=bytes(2 * 16000), rate=16000, width=2, channels=1))
    assert set(out) == {128}


# -- bridge wiring --------------------------------------------------------------------


class _RecordingCaller:
    def __init__(self, **config) -> None:
        self.config = _config(**config)
        self.configured = True
        self.audio: list[bytes] = []

    def announce(self, audio: bytes) -> SimpleNamespace:
        self.audio.append(audio)
        return SimpleNamespace(answered_by="101", attempted=["101"], error=None)


class _FakeTTS:
    def __init__(self) -> None:
        self.texts: list[str] = []

    async def synthesize(self, text: str) -> SynthesizedAudio:
        self.texts.append(text)
        return SynthesizedAudio(audio=bytes(1600), rate=8000, width=2, channels=1)


def _bridge(tmp_path, caller, tts=None, ha_notifier=None) -> UnifiBridge:
    return UnifiBridge(
        config=UnifiConfig(host="127.0.0.1", port=443, api_key="k"),
        session_store=JSONFileSessionStore(tmp_path),
        client=MagicMock(),
        tts_provider=tts,
        ha_notifier=ha_notifier,
        sip_caller=caller,
    )


async def _drain() -> None:
    await asyncio.sleep(0)
    current = asyncio.current_task()
    pending = [t for t in asyncio.all_tasks() if t is not current and not t.done()]
    if pending:
        await asyncio.gather(*pending)


def test_an_emergency_notification_also_rings_the_phone(tmp_path) -> None:
    caller, tts, notifier = _RecordingCaller(), _FakeTTS(), MagicMock()
    bridge = _bridge(tmp_path, caller, tts, notifier)

    async def scenario() -> None:
        await bridge._notify_household(
            "cam1", "Possible emergency at the door", category="emergency"
        )
        await _drain()

    asyncio.run(scenario())

    notifier.notify.assert_called_once()  # the push still goes out
    assert len(caller.audio) == 1
    assert tts.texts == ["This is KNOCK, about the front door. Possible emergency at the door"]


def test_categories_not_in_call_on_never_ring(tmp_path) -> None:
    caller, tts = _RecordingCaller(call_on=["emergency"]), _FakeTTS()
    bridge = _bridge(tmp_path, caller, tts)

    async def scenario() -> None:
        await bridge._notify_household("cam1", "A food delivery is here", category="approval")
        await _drain()

    asyncio.run(scenario())

    assert caller.audio == []


def test_a_second_emergency_within_the_cooldown_does_not_ring_again(tmp_path) -> None:
    caller, tts = _RecordingCaller(call_cooldown_seconds=300), _FakeTTS()
    bridge = _bridge(tmp_path, caller, tts)

    async def scenario() -> None:
        for _ in range(3):
            await bridge._notify_household("cam1", "Help!", category="emergency")
        await _drain()

    asyncio.run(scenario())

    assert len(caller.audio) == 1


def test_no_tts_means_no_call(tmp_path) -> None:
    caller = _RecordingCaller()
    bridge = _bridge(tmp_path, caller, tts=None)

    async def scenario() -> None:
        await bridge._notify_household("cam1", "Help!", category="emergency")
        await _drain()

    asyncio.run(scenario())

    assert caller.audio == []


def test_calls_work_without_home_assistant(tmp_path) -> None:
    caller, tts = _RecordingCaller(), _FakeTTS()
    bridge = _bridge(tmp_path, caller, tts, ha_notifier=None)

    async def scenario() -> None:
        await bridge._notify_household("cam1", "Help!", category="emergency")
        await _drain()

    asyncio.run(scenario())

    assert len(caller.audio) == 1


def test_a_failing_call_never_breaks_the_notification(tmp_path) -> None:
    caller, tts, notifier = _RecordingCaller(), _FakeTTS(), MagicMock()
    caller.announce = MagicMock(side_effect=RuntimeError("SIP server down"))  # type: ignore[method-assign]
    bridge = _bridge(tmp_path, caller, tts, notifier)

    async def scenario() -> None:
        await bridge._notify_household("cam1", "Help!", category="emergency")
        await _drain()

    asyncio.run(scenario())

    notifier.notify.assert_called_once()


# -- config ---------------------------------------------------------------------------


def test_sip_defaults_are_off_and_emergency_only() -> None:
    config = SipConfig()
    assert config.enabled is False
    assert config.call_on == ["emergency"]
    assert config.call_targets == []


def test_sip_from_env(monkeypatch) -> None:
    monkeypatch.setenv("KNOCK_SIP_ENABLED", "true")
    monkeypatch.setenv("KNOCK_SIP_SERVER", "talk.local")
    monkeypatch.setenv("KNOCK_SIP_USERNAME", "knock")
    monkeypatch.setenv("KNOCK_SIP_PASSWORD", "hunter2")
    monkeypatch.setenv("KNOCK_SIP_CALL_TARGETS", "101, 600")
    monkeypatch.setenv("KNOCK_SIP_CALL_ON", "emergency,approval")

    config = SipConfig.from_env()

    assert config.enabled is True
    assert config.server == "talk.local"
    assert config.password.get_secret_value() == "hunter2"
    assert "hunter2" not in repr(config)
    assert config.call_targets == ["101", "600"]
    assert config.call_on == ["emergency", "approval"]


def test_the_real_pyvoip_phone_can_be_constructed() -> None:
    # Catches a broken install (e.g. audioop missing on Python 3.13+)
    # without opening any sockets -- VoIPPhone only binds on start().
    from knock.integrations.sip_caller import _pyvoip_phone

    phone = _pyvoip_phone(_config(local_sip_port=0), "127.0.0.1")
    assert hasattr(phone, "call")
