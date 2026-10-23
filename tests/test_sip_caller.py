"""SIP announcement calls -- see `knock.integrations.sip_caller` and
`knock.integrations.baresip`.

The caller is tested against a scripted fake of baresip's control
interface. `test_real_baresip_call` at the bottom places a real call
between two baresip processes; it's opt-in (KNOCK_TEST_BARESIP=1) since it
needs baresip installed and free local ports.
"""

import array
import asyncio
import math
import os
import shutil
import socket
import subprocess
import time
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import knock.integrations.baresip as baresip_module
from knock.config import SipConfig, UnifiConfig
from knock.core.session_store import JSONFileSessionStore
from knock.integrations.baresip import (
    BaresipControl,
    BaresipProcess,
    NetstringReader,
    encode_netstring,
    render_accounts,
    render_config,
)
from knock.integrations.sip_caller import (
    SIP_SAMPLE_RATE,
    SipCaller,
    target_uri,
    to_sip_audio,
    write_announcement,
)
from knock.integrations.unifi import UnifiBridge
from knock.providers.tts.base import SynthesizedAudio


class _Clock:
    """Advances only when the fake control "waits" for an event."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class _FakeControl:
    """Scripted baresip control connection.

    `script` maps a dialed URI to the events that dial produces, each as
    (seconds_after_dial, event_dict). Waiting for an event advances the
    shared clock to the event's time, or by the full timeout if none is due.
    """

    def __init__(self, clock: _Clock, script: dict, register_events=()) -> None:
        self._clock = clock
        self._script = script
        self._queue: list[tuple[float, dict]] = [(0.0, e) for e in register_events]
        self.commands: list[tuple[str, str]] = []
        self.closed = False

    def command(self, name: str, params: str = "", timeout: float = 5.0) -> dict:
        self.commands.append((name, params))
        if name == "dial":
            if params not in self._script:
                return {"response": True, "ok": False, "data": "no route"}
            self._queue = [(self._clock.now + at, ev) for at, ev in self._script[params]]
        elif name == "hangup":
            self._queue = [
                (self._clock.now, {"event": True, "type": "CALL_CLOSED", "param": "user"})
            ]
        return {"response": True, "ok": True, "data": ""}

    def next_event(self, timeout: float):
        if self._queue and self._queue[0][0] <= self._clock.now + timeout:
            at, event = self._queue.pop(0)
            self._clock.now = max(self._clock.now, at)
            return event
        self._clock.now += timeout
        return None

    def close(self) -> None:
        self.closed = True


class _FakeBackend:
    def __init__(self, tmp_path: Path, control: _FakeControl, start_error=None) -> None:
        self.announcement_path = tmp_path / "announcement.wav"
        self._control = control
        self._start_error = start_error
        self.running = False
        self.starts = 0

    def start(self) -> None:
        self.starts += 1
        if self._start_error is not None:
            raise self._start_error
        self.running = True

    def connect(self) -> _FakeControl:
        return self._control

    def stop(self) -> None:
        self.running = False


def _event(kind: str, peer: str, param: str = "") -> dict:
    return {"event": True, "class": "call", "type": kind, "peeruri": peer, "param": param}


def _answers(uri: str, after: float = 1.0, plays_for: float = 2.0) -> list:
    """A phone that rings, picks up after `after`s, and gets baresip's own
    end-of-file hangup once the announcement has played.
    """
    return [
        (0.1, _event("CALL_RINGING", uri)),
        (after, _event("CALL_ESTABLISHED", uri)),
        (after + plays_for, _event("CALL_CLOSED", uri, "end of file")),
    ]


def _config(**overrides) -> SipConfig:
    defaults = {
        "enabled": True,
        "server": "192.0.2.10",
        "username": "knock",
        "password": "secret",
        "call_targets": ["101"],
        "ring_timeout_seconds": 20.0,
    }
    return SipConfig(**{**defaults, **overrides})


def _caller(tmp_path, script: dict, register_events=None, **config):
    clock = _Clock()
    if register_events is None:
        register_events = [{"event": True, "class": "register", "type": "REGISTER_OK"}]
    control = _FakeControl(clock, script, register_events)
    backend = _FakeBackend(tmp_path, control)
    return SipCaller(_config(**config), backend=backend, clock=clock), control, backend


# -- SipCaller ------------------------------------------------------------------------


def test_an_answered_call_plays_the_announcement_and_ends_by_itself(tmp_path) -> None:
    uri = "sip:101@192.0.2.10"
    caller, control, backend = _caller(tmp_path, {uri: _answers(uri)})

    outcome = caller.announce(b"\x10\x00" * 800)

    assert outcome.answered_by == "101"
    assert outcome.error is None
    assert control.commands == [("dial", uri)]  # no hangup: the WAV ending hangs up
    assert control.closed
    with wave.open(str(backend.announcement_path)) as wav:
        assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (8000, 1, 2)
        frames = wav.readframes(wav.getnframes())
    lead_in = frames[:-1600]
    assert lead_in and set(lead_in) == {0}  # silence before the first word
    assert frames.endswith(b"\x10\x00" * 800)


def test_a_phone_that_never_answers_gets_cancelled_and_the_next_is_tried(tmp_path) -> None:
    slow, fast = "sip:101@192.0.2.10", "sip:102@192.0.2.10"
    caller, control, _ = _caller(
        tmp_path,
        {slow: [(0.1, _event("CALL_RINGING", slow))], fast: _answers(fast)},
        call_targets=["101", "102"],
        ring_timeout_seconds=15.0,
    )

    outcome = caller.announce(b"\x00\x00")

    assert control.commands == [("dial", slow), ("hangup", ""), ("dial", fast)]
    assert outcome.attempted == ["101", "102"]
    assert outcome.answered_by == "102"


def test_once_someone_answers_nobody_else_is_called(tmp_path) -> None:
    first, second = "sip:101@192.0.2.10", "sip:102@192.0.2.10"
    caller, control, _ = _caller(
        tmp_path, {first: _answers(first), second: _answers(second)}, call_targets=["101", "102"]
    )

    outcome = caller.announce(b"\x00\x00")

    assert [c for c in control.commands if c[0] == "dial"] == [("dial", first)]
    assert outcome.answered_by == "101"


def test_busy_or_declined_moves_on_without_a_hangup(tmp_path) -> None:
    busy, ok = "sip:101@192.0.2.10", "sip:102@192.0.2.10"
    caller, control, _ = _caller(
        tmp_path,
        {busy: [(0.2, _event("CALL_CLOSED", busy, "486 Busy Here"))], ok: _answers(ok)},
        call_targets=["101", "102"],
    )

    outcome = caller.announce(b"\x00\x00")

    assert ("hangup", "") not in control.commands
    assert outcome.answered_by == "102"


def test_a_call_that_never_ends_is_hung_up_after_the_announcement(tmp_path) -> None:
    uri = "sip:101@192.0.2.10"
    stuck = [(0.1, _event("CALL_RINGING", uri)), (1.0, _event("CALL_ESTABLISHED", uri))]
    caller, control, _ = _caller(tmp_path, {uri: stuck})

    outcome = caller.announce(b"\x00\x00")

    assert outcome.answered_by == "101"
    assert control.commands[-1] == ("hangup", "")


def test_events_for_some_other_call_are_ignored(tmp_path) -> None:
    uri = "sip:101@192.0.2.10"
    script = {
        uri: [
            # Some other call (say, a stray inbound one) ending mid-ring
            # must not be read as "our call wasn't answered".
            (0.05, _event("CALL_CLOSED", "sip:999@192.0.2.10", "486 Busy Here")),
            *_answers(uri),
        ]
    }
    caller, _, _ = _caller(tmp_path, script)

    outcome = caller.announce(b"\x00\x00")

    assert outcome.answered_by == "101"


def test_a_rejected_login_is_reported_and_retried_next_time(tmp_path) -> None:
    fail = {"event": True, "class": "register", "type": "REGISTER_FAIL", "param": "403 Forbidden"}
    caller, control, backend = _caller(tmp_path, {}, register_events=[fail])

    outcome = caller.announce(b"\x00\x00")

    assert outcome.error is not None and "403 Forbidden" in outcome.error
    assert control.commands == []  # never dialed
    assert not backend.running  # stopped, so the next call restarts and re-registers
    caller.announce(b"\x00\x00")
    assert backend.starts == 2


def test_no_registration_needed_when_turned_off(tmp_path) -> None:
    uri = "sip:101@192.0.2.10"
    caller, _, _ = _caller(tmp_path, {uri: _answers(uri)}, register_events=[], registration=False)

    assert caller.announce(b"\x00\x00").answered_by == "101"


def test_baresip_stays_up_and_registered_between_calls(tmp_path) -> None:
    uri = "sip:101@192.0.2.10"
    caller, _, backend = _caller(tmp_path, {uri: _answers(uri)})

    caller.announce(b"\x00\x00")
    caller.announce(b"\x00\x00")

    assert backend.starts == 1


def test_a_missing_baresip_is_reported_not_raised(tmp_path) -> None:
    clock = _Clock()
    backend = _FakeBackend(
        tmp_path, _FakeControl(clock, {}), start_error=FileNotFoundError("no baresip")
    )
    caller = SipCaller(_config(), backend=backend, clock=clock)

    outcome = caller.announce(b"\x00\x00")

    assert outcome.error == "no baresip"
    assert caller._busy.acquire(blocking=False)  # the line was released


def test_only_one_call_at_a_time(tmp_path) -> None:
    caller, _, _ = _caller(tmp_path, {})
    assert caller._busy.acquire()

    assert caller.announce(b"\x00\x00").error == "another call is already in progress"


def test_unconfigured_caller_does_nothing(tmp_path) -> None:
    caller, _, backend = _caller(tmp_path, {}, call_targets=[])

    assert caller.announce(b"\x00\x00").error == "SIP calling isn't configured"
    assert backend.starts == 0


@pytest.mark.parametrize(
    "target,port,expected",
    [
        ("101", 5060, "sip:101@talk.local"),
        ("101", 5072, "sip:101@talk.local:5072"),
        ("600@other.example", 5060, "sip:600@other.example"),
        ("sip:600@other.example;transport=tcp", 5060, "sip:600@other.example;transport=tcp"),
    ],
)
def test_target_uri(target, port, expected) -> None:
    assert target_uri(target, "talk.local", port) == expected


# -- audio ----------------------------------------------------------------------------


def test_tts_audio_becomes_8khz_signed_16bit() -> None:
    rate = 22050
    tone = array.array(
        "h", [int(12000 * math.sin(2 * math.pi * 440 * i / rate)) for i in range(rate)]
    )

    out = array.array(
        "h", to_sip_audio(SynthesizedAudio(audio=tone.tobytes(), rate=rate, width=2, channels=1))
    )

    assert abs(len(out) - SIP_SAMPLE_RATE) <= 10  # one second at 8 kHz
    assert 10000 < max(out) < 13000  # level preserved


def test_announcement_length_includes_the_lead_in(tmp_path) -> None:
    seconds = write_announcement(tmp_path / "a.wav", bytes(2 * SIP_SAMPLE_RATE))
    assert seconds == pytest.approx(1.6)


# -- baresip config and control protocol -----------------------------------------------


def test_accounts_line_registers_with_the_login() -> None:
    line = render_accounts(_config(port=5060))
    assert line == "<sip:knock@192.0.2.10:5060>;auth_pass=secret;regint=600;answermode=manual\n"


def test_accounts_line_without_registration() -> None:
    assert ";regint=0;" in render_accounts(_config(registration=False))


def _mode_while_writing_password(tmp_path, monkeypatch) -> int | None:
    """The accounts file's permissions at the moment its contents (the SIP
    password) are rendered for writing, or None if it doesn't exist yet."""
    seen: list[int | None] = []
    real_render = baresip_module.render_accounts

    def probe(config: SipConfig) -> str:
        accounts = tmp_path / "accounts"
        seen.append(accounts.stat().st_mode & 0o777 if accounts.exists() else None)
        return real_render(config)

    monkeypatch.setattr(baresip_module, "render_accounts", probe)
    BaresipProcess(_config(), directory=tmp_path).write_config()
    return seen[0]


def test_accounts_file_is_owner_only_before_the_password_goes_in(tmp_path, monkeypatch) -> None:
    old_umask = os.umask(0)  # the most permissive default a process could have
    try:
        mode = _mode_while_writing_password(tmp_path, monkeypatch)
    finally:
        os.umask(old_umask)

    assert mode == 0o600
    accounts = tmp_path / "accounts"
    assert accounts.stat().st_mode & 0o777 == 0o600
    assert "auth_pass=secret" in accounts.read_text()


def test_an_existing_loose_accounts_file_is_tightened_before_rewriting(
    tmp_path, monkeypatch
) -> None:
    accounts = tmp_path / "accounts"
    accounts.write_text("old")
    accounts.chmod(0o644)

    assert _mode_while_writing_password(tmp_path, monkeypatch) == 0o600
    assert accounts.stat().st_mode & 0o777 == 0o600
    assert accounts.read_text() == render_accounts(_config())


@pytest.mark.parametrize("field", ["password", "username", "server"])
def test_values_that_could_inject_account_params_are_refused(field) -> None:
    with pytest.raises(ValueError, match=field):
        render_accounts(_config(**{field: "x;outbound=sip:evil.example"}))


def test_control_port_only_listens_on_loopback(tmp_path) -> None:
    config = render_config(_config(), tmp_path / "announcement.wav", 4444)
    assert "ctrl_tcp_listen 127.0.0.1:4444" in config
    assert f"audio_source aufile,{tmp_path / 'announcement.wav'}" in config


def test_netstrings_round_trip_across_split_reads() -> None:
    wire = encode_netstring({"event": True, "type": "CALL_RINGING"}) + encode_netstring({"x": 1})
    reader = NetstringReader()

    first = reader.feed(wire[:7])
    second = reader.feed(wire[7:])

    assert first == []
    assert second == [{"event": True, "type": "CALL_RINGING"}, {"x": 1}]


def test_control_client_keeps_events_that_arrive_before_a_response() -> None:
    server = socket.create_server(("127.0.0.1", 0))
    port = server.getsockname()[1]
    control = BaresipControl(port)
    conn, _ = server.accept()
    try:
        conn.sendall(encode_netstring({"event": True, "type": "CALL_RINGING"}))
        conn.sendall(encode_netstring({"response": True, "ok": True, "token": "knock-1"}))

        response = control.command("dial", "sip:101@x")
        event = control.next_event(timeout=1)

        assert response["ok"] is True
        assert event is not None and event["type"] == "CALL_RINGING"
        assert control.next_event(timeout=0.05) is None
    finally:
        control.close()
        conn.close()
        server.close()


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


# -- opt-in: a real call between two baresip processes -----------------------------------


@pytest.mark.skipif(
    os.environ.get("KNOCK_TEST_BARESIP") != "1" or shutil.which("baresip") is None,
    reason="opt-in: set KNOCK_TEST_BARESIP=1 with baresip installed",
)
def test_real_baresip_call(tmp_path) -> None:
    from knock.integrations.baresip import find_module_path

    # The far end: a second baresip that auto-answers and records what it hears.
    phone_dir, recordings = tmp_path / "phone", tmp_path / "recordings"
    phone_dir.mkdir()
    recordings.mkdir()
    silence = tmp_path / "silence.wav"
    with wave.open(str(silence), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(bytes(2 * 8000 * 10))
    module_path = find_module_path()
    (phone_dir / "config").write_text(
        (f"module_path {module_path}\n" if module_path else "")
        + "module g711.so\nmodule aufile.so\nmodule aubridge.so\nmodule sndfile.so\n"
        + "module menu.so\nmodule account.so\nsip_trans_def udp\n"
        + "sip_listen 127.0.0.1:15072\n"
        + f"audio_source aufile,{silence}\naudio_player aubridge,nil\n"
        + f"snd_path {recordings}\n"
    )
    (phone_dir / "accounts").write_text("<sip:door@127.0.0.1:15072>;regint=0;answermode=auto\n")
    phone = subprocess.Popen(
        ["baresip", "-f", str(phone_dir)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(1.5)
        from knock.integrations.baresip import BaresipProcess

        config = SipConfig(
            enabled=True,
            server="127.0.0.1",
            port=15072,
            username="knock",
            registration=False,
            local_ip="127.0.0.1",
            local_sip_port=15082,
            baresip_ctrl_port=14444,
            call_targets=["door"],
            ring_timeout_seconds=5,
        )
        caller = SipCaller(config, backend=BaresipProcess(config, directory=tmp_path / "knock"))
        rate = 22050
        tone = array.array(
            "h", [int(9000 * math.sin(2 * math.pi * 440 * i / rate)) for i in range(2 * rate)]
        )
        audio = to_sip_audio(SynthesizedAudio(audio=tone.tobytes(), rate=rate, width=2, channels=1))

        outcome = caller.announce(audio)
        caller.close()
    finally:
        phone.terminate()
        phone.wait(timeout=5)

    assert outcome.answered_by == "door", outcome
    heard = next(recordings.glob("*-dec.wav"))
    with wave.open(str(heard)) as wav:
        samples = array.array("h", wav.readframes(wav.getnframes()))
    loud = [i for i, x in enumerate(samples) if abs(x) > 2000]
    assert loud, "the far end heard nothing"
    assert loud[0] / 8000 == pytest.approx(0.6, abs=0.15)  # the lead-in came first
    segment = samples[loud[0] + 800 : loud[-1] - 800]
    crossings = sum(1 for i in range(1, len(segment)) if (segment[i - 1] < 0) != (segment[i] < 0))
    assert crossings / 2 / (len(segment) / 8000) == pytest.approx(440, abs=10)


def test_announce_rings_only_the_given_targets_when_a_door_overrides_them(tmp_path) -> None:
    uri = "sip:101@192.0.2.10"
    caller, control, _ = _caller(tmp_path, {uri: _answers(uri)})

    outcome = caller.announce(b"\x10\x00" * 800, targets=["101"])

    assert outcome.attempted == ["101"]
    assert control.commands == [("dial", uri)]
