import asyncio
import wave
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from uiprotect import EventChange

from knock.config import UnifiConfig
from knock.conversation.prompts import GREETING
from knock.core.session_store import JSONFileSessionStore
from knock.core.state import SessionState
from knock.integrations.unifi import _THINKING_PHRASE, UnifiBridge, _default_session_id
from knock.providers.tts.base import SynthesizedAudio


def _event(
    event_type: str = "ring",
    device_id: str = "cam1",
    smart_detect_types: tuple[str, ...] = (),
) -> SimpleNamespace:
    return SimpleNamespace(
        type=event_type, device_id=device_id, smart_detect_types=smart_detect_types
    )


def _bridge(tmp_path, mock_client: MagicMock | None = None, **config_overrides) -> UnifiBridge:
    defaults = {"host": "127.0.0.1", "port": 443, "api_key": "test-key"}
    config = UnifiConfig(**{**defaults, **config_overrides})
    return UnifiBridge(
        config=config,
        session_store=JSONFileSessionStore(tmp_path),
        client=mock_client or MagicMock(),
    )


async def _settle() -> None:
    """Let any tasks `_on_event` scheduled onto this loop actually run."""
    await asyncio.sleep(0)
    current = asyncio.current_task()
    pending = [t for t in asyncio.all_tasks() if t is not current and not t.done()]
    if pending:
        await asyncio.gather(*pending)


# -- default_session_id --------------------------------------------------------


def test_default_session_id_sanitizes_unsafe_characters() -> None:
    assert _default_session_id("cam/1") == "unifi-cam_1"
    assert _default_session_id("") == "unifi-unknown"


# -- should_trigger --------------------------------------------------------------


def test_should_trigger_on_default_ring(tmp_path) -> None:
    bridge = _bridge(tmp_path)
    assert bridge.should_trigger(_event(event_type="ring")) is True


def test_should_trigger_ignores_unrelated_event_type(tmp_path) -> None:
    bridge = _bridge(tmp_path)
    assert bridge.should_trigger(_event(event_type="motion")) is False


def test_should_trigger_on_configured_smart_detect_type(tmp_path) -> None:
    bridge = _bridge(tmp_path, trigger_on=["ring", "person"])
    event = _event(event_type="smartDetectZone", smart_detect_types=("person",))
    assert bridge.should_trigger(event) is True


def test_should_trigger_ignores_unmatched_smart_detect_type(tmp_path) -> None:
    bridge = _bridge(tmp_path, trigger_on=["ring", "person"])
    event = _event(event_type="smartDetectZone", smart_detect_types=("vehicle",))
    assert bridge.should_trigger(event) is False


# -- build_event --------------------------------------------------------------


def test_build_event_for_ring(tmp_path) -> None:
    bridge = _bridge(tmp_path)
    event = bridge.build_event(_event(event_type="ring", device_id="front-door"))
    assert event.source == "unifi-front-door"
    assert "Doorbell ring" in event.text
    assert "front-door" in event.text


def test_build_event_for_smart_detect(tmp_path) -> None:
    bridge = _bridge(tmp_path)
    event = bridge.build_event(
        _event(event_type="smartDetectZone", device_id="front-door", smart_detect_types=("person",))
    )
    assert "person" in event.text
    assert "detected" in event.text


# -- handle_event (async) -------------------------------------------------------


def test_handle_event_persists_session_for_triggering_event(tmp_path) -> None:
    bridge = _bridge(tmp_path)
    decision = asyncio.run(bridge.handle_event(_event()))

    assert decision is not None
    state = bridge.session_store.load("unifi-cam1")
    assert state is not None
    assert state.turn_count == 1


def test_handle_event_returns_none_for_non_triggering_event(tmp_path) -> None:
    bridge = _bridge(tmp_path)
    decision = asyncio.run(bridge.handle_event(_event(event_type="motion")))

    assert decision is None
    assert bridge.session_store.load("unifi-cam1") is None


def test_handle_event_enriches_with_vision_description(tmp_path) -> None:
    mock_client = MagicMock()
    mock_client.get_public_api_camera_snapshot = AsyncMock(return_value=b"fake-jpeg-bytes")
    bridge = _bridge(tmp_path, mock_client=mock_client)
    vision_provider = MagicMock()
    vision_provider.describe.return_value = "a person at the door"
    bridge.vision_provider = vision_provider

    asyncio.run(bridge.handle_event(_event()))

    mock_client.get_public_api_camera_snapshot.assert_awaited_once_with("cam1")
    vision_provider.describe.assert_called_once_with(b"fake-jpeg-bytes")


def test_handle_event_skips_vision_when_snapshot_is_none(tmp_path) -> None:
    mock_client = MagicMock()
    mock_client.get_public_api_camera_snapshot = AsyncMock(return_value=None)
    bridge = _bridge(tmp_path, mock_client=mock_client)
    vision_provider = MagicMock()
    bridge.vision_provider = vision_provider

    decision = asyncio.run(bridge.handle_event(_event()))

    assert decision is not None
    vision_provider.describe.assert_not_called()


def test_handle_event_survives_vision_failure(tmp_path) -> None:
    mock_client = MagicMock()
    mock_client.get_public_api_camera_snapshot = AsyncMock(side_effect=RuntimeError("boom"))
    bridge = _bridge(tmp_path, mock_client=mock_client)
    vision_provider = MagicMock()
    bridge.vision_provider = vision_provider

    decision = asyncio.run(bridge.handle_event(_event()))

    assert decision is not None


# -- _on_event (sync-callback -> scheduled task) ---------------------------------


def test_on_event_schedules_handling_on_started(tmp_path) -> None:
    bridge = _bridge(tmp_path)

    async def scenario() -> None:
        bridge._on_event(_event(), EventChange.STARTED)
        await _settle()

    asyncio.run(scenario())

    assert bridge.session_store.load("unifi-cam1") is not None


@pytest.mark.parametrize("change", [EventChange.UPDATED, EventChange.ENDED, EventChange.REMOVED])
def test_on_event_ignores_non_started_changes(tmp_path, change) -> None:
    bridge = _bridge(tmp_path)

    async def scenario() -> None:
        bridge._on_event(_event(), change)
        await _settle()

    asyncio.run(scenario())

    assert bridge.session_store.load("unifi-cam1") is None


# -- config ---------------------------------------------------------------------


def test_unifi_config_defaults() -> None:
    config = UnifiConfig()
    assert config.host == "127.0.0.1"
    assert config.port == 443
    assert config.verify_ssl is False
    assert config.trigger_on == ["ring"]


def test_unifi_config_from_env(monkeypatch) -> None:
    monkeypatch.setenv("KNOCK_UNIFI_HOST", "protect.local")
    monkeypatch.setenv("KNOCK_UNIFI_API_KEY", "abc123")
    monkeypatch.setenv("KNOCK_UNIFI_TRIGGER_ON", "ring, person")
    monkeypatch.setenv("KNOCK_UNIFI_VERIFY_SSL", "true")

    config = UnifiConfig.from_env()

    assert config.host == "protect.local"
    assert config.api_key.get_secret_value() == "abc123"
    assert config.trigger_on == ["ring", "person"]
    assert config.verify_ssl is True


# -- speak_to_visitor (talkback) --------------------------------------------------


def _with_bootstrap_camera(mock_client: MagicMock, device_id: str, camera: MagicMock) -> None:
    """`UnifiConfig` is api_key-only, so `self.client` is a public-only
    `ProtectApiClient` -- `listen_to_visitor`/`speak_to_visitor` read a
    camera from `public_bootstrap.cameras` (kept primed by `update_public()`)
    rather than the private `get_camera()`, which these tests' `mock_client`
    otherwise has no stubbed behavior for.
    """
    mock_client.public_bootstrap.cameras = {device_id: camera}


def _recording_stream_factory(captured: dict):
    """Reads the WAV file while its temp dir is still alive, records args."""

    def factory(camera, content_url, session):
        with wave.open(content_url, "rb") as wav_file:
            captured["channels"] = wav_file.getnchannels()
            captured["rate"] = wav_file.getframerate()
            captured["sampwidth"] = wav_file.getsampwidth()
            captured["frames"] = wav_file.readframes(wav_file.getnframes())
        captured["camera"] = camera
        captured["session"] = session

        fake_stream = MagicMock()
        fake_stream.run_until_complete = AsyncMock()
        captured["stream"] = fake_stream
        return fake_stream

    return factory


def _synthesized_audio() -> SynthesizedAudio:
    return SynthesizedAudio(audio=b"\x01\x02\x03\x04", rate=22050, width=2, channels=1)


def test_speak_to_visitor_is_a_no_op_without_tts_provider(tmp_path) -> None:
    bridge = UnifiBridge(
        config=UnifiConfig(host="127.0.0.1", port=443, api_key="k"),
        session_store=JSONFileSessionStore(tmp_path),
        client=MagicMock(),
    )
    asyncio.run(bridge.speak_to_visitor("cam1", "hello"))  # should not raise


def test_speak_to_visitor_streams_synthesized_audio(tmp_path) -> None:
    mock_client = MagicMock()
    fake_camera = MagicMock()
    fake_camera.feature_flags.has_speaker = True
    _with_bootstrap_camera(mock_client, "cam1", fake_camera)
    mock_client.create_talkback_session_public = AsyncMock(return_value="fake-session")

    tts_provider = MagicMock()
    tts_provider.synthesize = AsyncMock(return_value=_synthesized_audio())

    captured: dict = {}
    bridge = UnifiBridge(
        config=UnifiConfig(host="127.0.0.1", port=443, api_key="k"),
        session_store=JSONFileSessionStore(tmp_path),
        client=mock_client,
        tts_provider=tts_provider,
        talkback_stream_factory=_recording_stream_factory(captured),
    )

    asyncio.run(bridge.speak_to_visitor("cam1", "Thanks, you can leave the package."))

    tts_provider.synthesize.assert_awaited_once_with("Thanks, you can leave the package.")
    assert captured["camera"] is fake_camera
    assert captured["session"] == "fake-session"
    assert captured["channels"] == 1
    assert captured["rate"] == 22050
    assert captured["sampwidth"] == 2
    assert captured["frames"] == b"\x01\x02\x03\x04"
    captured["stream"].run_until_complete.assert_awaited_once()


def test_speak_to_visitor_skips_cameras_without_a_speaker(tmp_path) -> None:
    mock_client = MagicMock()
    fake_camera = MagicMock()
    fake_camera.feature_flags.has_speaker = False
    _with_bootstrap_camera(mock_client, "cam1", fake_camera)

    tts_provider = MagicMock()
    tts_provider.synthesize = AsyncMock(return_value=_synthesized_audio())

    stream_factory = MagicMock()
    bridge = UnifiBridge(
        config=UnifiConfig(host="127.0.0.1", port=443, api_key="k"),
        session_store=JSONFileSessionStore(tmp_path),
        client=mock_client,
        tts_provider=tts_provider,
        talkback_stream_factory=stream_factory,
    )

    asyncio.run(bridge.speak_to_visitor("cam1", "hello"))

    stream_factory.assert_not_called()


def test_speak_to_visitor_falls_back_when_public_session_unavailable(tmp_path) -> None:
    mock_client = MagicMock()
    fake_camera = MagicMock()
    fake_camera.feature_flags.has_speaker = True
    _with_bootstrap_camera(mock_client, "cam1", fake_camera)
    mock_client.create_talkback_session_public = AsyncMock(
        side_effect=RuntimeError("no public api")
    )

    tts_provider = MagicMock()
    tts_provider.synthesize = AsyncMock(return_value=_synthesized_audio())

    captured: dict = {}
    bridge = UnifiBridge(
        config=UnifiConfig(host="127.0.0.1", port=443, api_key="k"),
        session_store=JSONFileSessionStore(tmp_path),
        client=mock_client,
        tts_provider=tts_provider,
        talkback_stream_factory=_recording_stream_factory(captured),
    )

    asyncio.run(bridge.speak_to_visitor("cam1", "hello"))

    assert captured["session"] is None
    captured["stream"].run_until_complete.assert_awaited_once()


def test_speak_to_visitor_survives_synthesis_failure(tmp_path) -> None:
    mock_client = MagicMock()
    tts_provider = MagicMock()
    tts_provider.synthesize = AsyncMock(side_effect=RuntimeError("tts down"))
    stream_factory = MagicMock()

    bridge = UnifiBridge(
        config=UnifiConfig(host="127.0.0.1", port=443, api_key="k"),
        session_store=JSONFileSessionStore(tmp_path),
        client=mock_client,
        tts_provider=tts_provider,
        talkback_stream_factory=stream_factory,
    )

    asyncio.run(bridge.speak_to_visitor("cam1", "hello"))  # should not raise

    stream_factory.assert_not_called()


def test_speak_to_visitor_survives_stream_failure(tmp_path) -> None:
    mock_client = MagicMock()
    fake_camera = MagicMock()
    fake_camera.feature_flags.has_speaker = True
    _with_bootstrap_camera(mock_client, "cam1", fake_camera)
    mock_client.create_talkback_session_public = AsyncMock(return_value=None)

    tts_provider = MagicMock()
    tts_provider.synthesize = AsyncMock(return_value=_synthesized_audio())

    failing_stream = MagicMock()
    failing_stream.run_until_complete = AsyncMock(side_effect=RuntimeError("stream died"))
    stream_factory = MagicMock(return_value=failing_stream)

    bridge = UnifiBridge(
        config=UnifiConfig(host="127.0.0.1", port=443, api_key="k"),
        session_store=JSONFileSessionStore(tmp_path),
        client=mock_client,
        tts_provider=tts_provider,
        talkback_stream_factory=stream_factory,
    )

    asyncio.run(bridge.speak_to_visitor("cam1", "hello"))  # should not raise


def test_handle_event_greets_before_speaking_the_response_on_first_turn(tmp_path) -> None:
    mock_client = MagicMock()
    fake_camera = MagicMock()
    fake_camera.feature_flags.has_speaker = True
    _with_bootstrap_camera(mock_client, "cam1", fake_camera)
    mock_client.create_talkback_session_public = AsyncMock(return_value=None)

    tts_provider = MagicMock()
    tts_provider.synthesize = AsyncMock(return_value=_synthesized_audio())

    def stream_factory(camera, content_url, session):
        stream = MagicMock()
        stream.run_until_complete = AsyncMock()
        return stream

    bridge = UnifiBridge(
        config=UnifiConfig(host="127.0.0.1", port=443, api_key="k"),
        session_store=JSONFileSessionStore(tmp_path),
        client=mock_client,
        tts_provider=tts_provider,
        talkback_stream_factory=stream_factory,
    )

    decision = asyncio.run(bridge.handle_event(_event()))

    assert decision is not None
    assert tts_provider.synthesize.await_count == 2
    first_text = tts_provider.synthesize.await_args_list[0].args[0]
    second_text = tts_provider.synthesize.await_args_list[1].args[0]
    assert first_text == GREETING
    assert second_text == decision.text
    # The orchestrator must not *also* prepend the greeting to the spoken
    # response -- it was already said aloud above.
    assert GREETING not in decision.text


def test_handle_event_always_regreets_on_a_fresh_ring(tmp_path) -> None:
    # A ring is as clear a "this is a new visit" signal as exists, so it
    # always restarts the session and re-greets -- even if the same
    # camera's session already has turns on it from an earlier, unrelated
    # visit.
    mock_client = MagicMock()
    fake_camera = MagicMock()
    fake_camera.feature_flags.has_speaker = True
    _with_bootstrap_camera(mock_client, "cam1", fake_camera)
    mock_client.create_talkback_session_public = AsyncMock(return_value=None)

    tts_provider = MagicMock()
    tts_provider.synthesize = AsyncMock(return_value=_synthesized_audio())

    def stream_factory(camera, content_url, session):
        stream = MagicMock()
        stream.run_until_complete = AsyncMock()
        return stream

    bridge = UnifiBridge(
        config=UnifiConfig(host="127.0.0.1", port=443, api_key="k"),
        session_store=JSONFileSessionStore(tmp_path),
        client=mock_client,
        tts_provider=tts_provider,
        talkback_stream_factory=stream_factory,
    )

    asyncio.run(bridge.handle_event(_event(event_type="ring")))
    tts_provider.synthesize.reset_mock()
    asyncio.run(bridge.handle_event(_event(event_type="ring")))

    # Greeting + response, both turns -- the ring reset the session instead
    # of continuing turn 2 without a greeting.
    assert tts_provider.synthesize.await_count == 2
    final_state = bridge.session_store.load("unifi-cam1")
    assert final_state is not None
    assert final_state.turn_count == 1


def test_handle_event_does_not_regreet_for_a_repeated_non_ring_trigger(tmp_path) -> None:
    # A lingering smart-detect trigger (e.g. "person") can fire repeatedly
    # during the *same* visit -- unlike a ring, it shouldn't restart the
    # session or re-greet each time.
    mock_client = MagicMock()
    fake_camera = MagicMock()
    fake_camera.feature_flags.has_speaker = True
    _with_bootstrap_camera(mock_client, "cam1", fake_camera)
    mock_client.create_talkback_session_public = AsyncMock(return_value=None)

    tts_provider = MagicMock()
    tts_provider.synthesize = AsyncMock(return_value=_synthesized_audio())

    def stream_factory(camera, content_url, session):
        stream = MagicMock()
        stream.run_until_complete = AsyncMock()
        return stream

    bridge = UnifiBridge(
        config=UnifiConfig(host="127.0.0.1", port=443, api_key="k", trigger_on=["ring", "person"]),
        session_store=JSONFileSessionStore(tmp_path),
        client=mock_client,
        tts_provider=tts_provider,
        talkback_stream_factory=stream_factory,
    )
    event = _event(event_type="smartDetectZone", smart_detect_types=("person",))

    asyncio.run(bridge.handle_event(event))
    tts_provider.synthesize.reset_mock()
    asyncio.run(bridge.handle_event(event))

    assert tts_provider.synthesize.await_count == 1
    final_state = bridge.session_store.load("unifi-cam1")
    assert final_state is not None
    assert final_state.turn_count == 2


def test_handle_event_restarts_a_stale_session_even_without_a_ring(tmp_path) -> None:
    mock_client = MagicMock()
    fake_camera = MagicMock()
    fake_camera.feature_flags.has_speaker = True
    _with_bootstrap_camera(mock_client, "cam1", fake_camera)
    mock_client.create_talkback_session_public = AsyncMock(return_value=None)

    tts_provider = MagicMock()
    tts_provider.synthesize = AsyncMock(return_value=_synthesized_audio())

    def stream_factory(camera, content_url, session):
        stream = MagicMock()
        stream.run_until_complete = AsyncMock()
        return stream

    session_store = JSONFileSessionStore(tmp_path)
    stale = SessionState(
        session_id="unifi-cam1",
        turn_count=3,
        updated_at=datetime.now(UTC) - timedelta(hours=1),
    )
    session_store.save(stale)

    bridge = UnifiBridge(
        config=UnifiConfig(host="127.0.0.1", port=443, api_key="k", trigger_on=["ring", "person"]),
        session_store=session_store,
        client=mock_client,
        tts_provider=tts_provider,
        talkback_stream_factory=stream_factory,
    )
    event = _event(event_type="smartDetectZone", smart_detect_types=("person",))

    asyncio.run(bridge.handle_event(event))

    assert tts_provider.synthesize.await_count == 2
    final_state = session_store.load("unifi-cam1")
    assert final_state is not None
    assert final_state.turn_count == 1


def test_handle_event_greets_before_listening_for_the_visitors_reply(tmp_path) -> None:
    events: list[str] = []

    mock_client = MagicMock()
    fake_camera = MagicMock()
    fake_camera.feature_flags.has_speaker = True
    fake_camera.rtsps_streams = _fake_rtsp_streams("rtsps://console/high")
    _with_bootstrap_camera(mock_client, "cam1", fake_camera)
    mock_client.create_talkback_session_public = AsyncMock(return_value=None)

    tts_provider = MagicMock()

    async def synthesize(text: str) -> SynthesizedAudio:
        events.append(f"spoke:{text}")
        return _synthesized_audio()

    tts_provider.synthesize = synthesize

    stt_provider = MagicMock()
    # "hello" once (the visitor's actual reply), then silence -- ends the
    # conversation after one round trip. This test is about *ordering*
    # (greet before listen), not conversation length.
    stt_provider.transcribe = AsyncMock(side_effect=["hello", ""])

    def capture(url: str, duration: float, rate: int, verify_ssl: bool) -> bytes:
        events.append("listened")
        return b"\x01\x02"

    def stream_factory(camera, content_url, session):
        stream = MagicMock()
        stream.run_until_complete = AsyncMock()
        return stream

    bridge = UnifiBridge(
        config=UnifiConfig(host="127.0.0.1", port=443, api_key="k"),
        session_store=JSONFileSessionStore(tmp_path),
        client=mock_client,
        tts_provider=tts_provider,
        stt_provider=stt_provider,
        rtsp_audio_capture=capture,
        talkback_stream_factory=stream_factory,
    )

    asyncio.run(bridge.handle_event(_event()))

    assert events[0] == f"spoke:{GREETING}"
    assert events[1] == "listened"


def _conversational_bridge(
    tmp_path, *, transcripts: list[str], tts_text_log: list[str], ha_notifier=None
):
    mock_client = MagicMock()
    fake_camera = MagicMock()
    fake_camera.feature_flags.has_speaker = True
    fake_camera.rtsps_streams = _fake_rtsp_streams("rtsps://console/high")
    _with_bootstrap_camera(mock_client, "cam1", fake_camera)
    mock_client.create_talkback_session_public = AsyncMock(return_value=None)

    tts_provider = MagicMock()

    async def synthesize(text: str) -> SynthesizedAudio:
        tts_text_log.append(text)
        return _synthesized_audio()

    tts_provider.synthesize = synthesize

    stt_provider = MagicMock()
    stt_provider.transcribe = AsyncMock(side_effect=transcripts)

    def stream_factory(camera, content_url, session):
        stream = MagicMock()
        stream.run_until_complete = AsyncMock()
        return stream

    return UnifiBridge(
        config=UnifiConfig(host="127.0.0.1", port=443, api_key="k"),
        session_store=JSONFileSessionStore(tmp_path),
        client=mock_client,
        tts_provider=tts_provider,
        stt_provider=stt_provider,
        rtsp_audio_capture=lambda url, duration, rate, verify_ssl: b"\x01\x02",
        talkback_stream_factory=stream_factory,
        ha_notifier=ha_notifier,
    )


def test_handle_event_continues_the_conversation_while_the_visitor_keeps_talking(
    tmp_path,
) -> None:
    tts_text_log: list[str] = []
    bridge = _conversational_bridge(
        tmp_path,
        transcripts=[
            "I have a package for you",
            "this package needs a signature",
            "",
        ],
        tts_text_log=tts_text_log,
    )

    decision = asyncio.run(bridge.handle_event(_event()))

    assert decision is not None
    # Greeting, then one spoken response per turn (delivery, then
    # signature-required) -- the empty third transcript ends it there.
    assert tts_text_log[0] == GREETING
    assert tts_text_log.count(_THINKING_PHRASE) == 2
    responses = [text for text in tts_text_log if text not in (GREETING, _THINKING_PHRASE)]
    assert len(responses) == 2
    assert "leave the package" in responses[0].lower()
    assert "sign" in responses[1].lower()

    final_state = bridge.session_store.load("unifi-cam1")
    assert final_state is not None
    assert final_state.turn_count == 2


def test_handle_event_stops_at_the_conversation_turn_cap(tmp_path) -> None:
    tts_text_log: list[str] = []
    # Always has something to say -- without a cap this would loop forever.
    bridge = _conversational_bridge(
        tmp_path,
        transcripts=["are we still talking"] * 10,
        tts_text_log=tts_text_log,
    )

    decision = asyncio.run(bridge.handle_event(_event()))

    assert decision is not None
    final_state = bridge.session_store.load("unifi-cam1")
    assert final_state is not None
    assert final_state.turn_count == 5


def test_handle_event_stops_the_conversation_on_escalation(tmp_path) -> None:
    tts_text_log: list[str] = []
    # If the loop didn't stop here, the next (unused) transcript would
    # prove it kept going.
    bridge = _conversational_bridge(
        tmp_path,
        transcripts=["there's a fire, help!", "should never be heard"],
        tts_text_log=tts_text_log,
    )

    decision = asyncio.run(bridge.handle_event(_event()))

    assert decision is not None
    assert decision.escalate is True
    final_state = bridge.session_store.load("unifi-cam1")
    assert final_state is not None
    assert final_state.turn_count == 1


def test_handle_event_notifies_home_assistant_on_a_signature_required_delivery(
    tmp_path,
) -> None:
    ha_notifier = MagicMock()
    bridge = _conversational_bridge(
        tmp_path,
        transcripts=["this package needs a signature", ""],
        tts_text_log=[],
        ha_notifier=ha_notifier,
    )

    asyncio.run(bridge.handle_event(_event()))

    ha_notifier.notify.assert_called_once()
    assert "signature" in ha_notifier.notify.call_args.args[0].lower()


def test_handle_event_does_not_notify_home_assistant_for_a_plain_delivery(tmp_path) -> None:
    ha_notifier = MagicMock()
    bridge = _conversational_bridge(
        tmp_path,
        transcripts=["I have a package for you", ""],
        tts_text_log=[],
        ha_notifier=ha_notifier,
    )

    asyncio.run(bridge.handle_event(_event()))

    ha_notifier.notify.assert_not_called()


def test_handle_event_survives_a_failing_ha_notifier(tmp_path) -> None:
    ha_notifier = MagicMock()
    ha_notifier.notify.side_effect = RuntimeError("home assistant is down")
    bridge = _conversational_bridge(
        tmp_path,
        transcripts=["this package needs a signature", ""],
        tts_text_log=[],
        ha_notifier=ha_notifier,
    )

    decision = asyncio.run(bridge.handle_event(_event()))

    assert decision is not None  # should not raise


def test_handle_event_is_fine_without_an_ha_notifier_configured(tmp_path) -> None:
    bridge = _conversational_bridge(
        tmp_path,
        transcripts=["this package needs a signature", ""],
        tts_text_log=[],
    )

    decision = asyncio.run(bridge.handle_event(_event()))

    assert decision is not None  # should not raise


# -- _capture_rtsp_audio (real PyAV decode, no network) --------------------------


def test_capture_rtsp_audio_resamples_to_mono_16bit_16khz(tmp_path) -> None:
    import math
    import struct

    from knock.integrations.unifi import _capture_rtsp_audio

    rate_in = 8000
    n = rate_in  # 1 second
    samples = [int(3000 * math.sin(2 * math.pi * 440 * i / rate_in)) for i in range(n)]
    pcm_in = struct.pack(f"<{n}h", *samples)
    wav_path = tmp_path / "tone.wav"
    with wave.open(str(wav_path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(rate_in)
        f.writeframes(pcm_in)

    pcm_out = _capture_rtsp_audio(str(wav_path), duration=5.0, sample_rate=16000)

    assert isinstance(pcm_out, bytes)
    # 1s of 8kHz input resampled to 16kHz 16-bit mono -> ~32000 bytes
    assert 30000 < len(pcm_out) < 36000


def test_capture_rtsp_audio_disables_tls_verification_when_requested(monkeypatch) -> None:
    # UniFi's local console uses a self-signed cert for RTSPS by default;
    # without this, PyAV/FFmpeg rejects the connection outright with
    # "[Errno 5] Input/output error" (confirmed against a real console --
    # FFmpeg's own verbose log says "Peer certificate failed verification",
    # which that generic exception message never surfaces).
    import knock.integrations.unifi as unifi_module

    captured_options: dict = {}

    class _FakeContainer:
        def __enter__(self):
            raise RuntimeError("stop before decoding -- only options need checking")

        def __exit__(self, *exc_info):
            return False

    def fake_open(url, timeout=None, options=None):
        captured_options.update(options or {})
        return _FakeContainer()

    monkeypatch.setattr(unifi_module.av, "open", fake_open)

    try:
        unifi_module._capture_rtsp_audio("rtsps://console/high", 1.0, 16000, verify_ssl=False)
    except RuntimeError:
        pass

    assert captured_options.get("tls_verify") == "0"


def test_capture_rtsp_audio_leaves_tls_verification_on_by_default(monkeypatch) -> None:
    import knock.integrations.unifi as unifi_module

    captured_options: dict = {}

    class _FakeContainer:
        def __enter__(self):
            raise RuntimeError("stop before decoding -- only options need checking")

        def __exit__(self, *exc_info):
            return False

    def fake_open(url, timeout=None, options=None):
        captured_options.update(options or {})
        return _FakeContainer()

    monkeypatch.setattr(unifi_module.av, "open", fake_open)

    try:
        unifi_module._capture_rtsp_audio("rtsps://console/high", 1.0, 16000, verify_ssl=True)
    except RuntimeError:
        pass

    assert "tls_verify" not in captured_options


def test_capture_rtsp_audio_raises_for_a_missing_source(tmp_path) -> None:
    import av

    from knock.integrations.unifi import _capture_rtsp_audio

    with pytest.raises(av.FFmpegError):
        _capture_rtsp_audio(str(tmp_path / "does-not-exist.wav"), duration=1.0, sample_rate=16000)


# -- listen_to_visitor (STT capture) ----------------------------------------------


def _fake_rtsp_streams(url: str | None):
    streams = MagicMock()
    streams.get_stream_url.return_value = url
    return streams


def test_listen_to_visitor_is_a_no_op_without_stt_provider(tmp_path) -> None:
    bridge = UnifiBridge(
        config=UnifiConfig(host="127.0.0.1", port=443, api_key="k"),
        session_store=JSONFileSessionStore(tmp_path),
        client=MagicMock(),
    )
    assert asyncio.run(bridge.listen_to_visitor("cam1")) == ""


def test_listen_to_visitor_transcribes_captured_audio(tmp_path) -> None:
    mock_client = MagicMock()
    fake_camera = MagicMock()
    fake_camera.rtsps_streams = _fake_rtsp_streams("rtsps://console/high?enableSrtp")
    _with_bootstrap_camera(mock_client, "cam1", fake_camera)

    stt_provider = MagicMock()
    stt_provider.transcribe = AsyncMock(return_value="Hi, I have an Amazon package")

    capture = MagicMock(return_value=b"\x01\x02\x03\x04")
    bridge = UnifiBridge(
        config=UnifiConfig(host="127.0.0.1", port=443, api_key="k", listen_seconds=4.0),
        session_store=JSONFileSessionStore(tmp_path),
        client=mock_client,
        stt_provider=stt_provider,
        rtsp_audio_capture=capture,
    )

    transcript = asyncio.run(bridge.listen_to_visitor("cam1"))

    assert transcript == "Hi, I have an Amazon package"
    capture.assert_called_once_with("rtsps://console/high?enableSrtp", 4.0, 16000, False)
    stt_provider.transcribe.assert_awaited_once_with(
        b"\x01\x02\x03\x04", rate=16000, width=2, channels=1
    )


def test_listen_to_visitor_returns_empty_without_an_rtsp_stream(tmp_path) -> None:
    mock_client = MagicMock()
    fake_camera = MagicMock()
    fake_camera.rtsps_streams = _fake_rtsp_streams(None)
    _with_bootstrap_camera(mock_client, "cam1", fake_camera)

    stt_provider = MagicMock()
    stt_provider.transcribe = AsyncMock()
    capture = MagicMock()

    bridge = UnifiBridge(
        config=UnifiConfig(host="127.0.0.1", port=443, api_key="k"),
        session_store=JSONFileSessionStore(tmp_path),
        client=mock_client,
        stt_provider=stt_provider,
        rtsp_audio_capture=capture,
    )

    assert asyncio.run(bridge.listen_to_visitor("cam1")) == ""
    capture.assert_not_called()
    stt_provider.transcribe.assert_not_awaited()


def test_listen_to_visitor_survives_capture_failure(tmp_path) -> None:
    mock_client = MagicMock()
    fake_camera = MagicMock()
    fake_camera.rtsps_streams = _fake_rtsp_streams("rtsps://console/high")
    _with_bootstrap_camera(mock_client, "cam1", fake_camera)

    stt_provider = MagicMock()
    stt_provider.transcribe = AsyncMock()
    capture = MagicMock(side_effect=RuntimeError("stream unavailable"))

    bridge = UnifiBridge(
        config=UnifiConfig(host="127.0.0.1", port=443, api_key="k"),
        session_store=JSONFileSessionStore(tmp_path),
        client=mock_client,
        stt_provider=stt_provider,
        rtsp_audio_capture=capture,
    )

    assert asyncio.run(bridge.listen_to_visitor("cam1")) == ""
    stt_provider.transcribe.assert_not_awaited()


def test_handle_event_uses_transcript_as_event_text_when_available(tmp_path) -> None:
    mock_client = MagicMock()
    fake_camera = MagicMock()
    fake_camera.rtsps_streams = _fake_rtsp_streams("rtsps://console/high")
    _with_bootstrap_camera(mock_client, "cam1", fake_camera)

    stt_provider = MagicMock()
    stt_provider.transcribe = AsyncMock(return_value="Hi, I have an Amazon package")
    capture = MagicMock(return_value=b"\x01\x02")

    bridge = UnifiBridge(
        config=UnifiConfig(host="127.0.0.1", port=443, api_key="k"),
        session_store=JSONFileSessionStore(tmp_path),
        client=mock_client,
        stt_provider=stt_provider,
        rtsp_audio_capture=capture,
    )

    decision = asyncio.run(bridge.handle_event(_event()))

    assert decision is not None
    assert "leave the package" in decision.text.lower()


def test_handle_event_falls_back_to_generic_text_when_transcript_is_empty(tmp_path) -> None:
    mock_client = MagicMock()
    fake_camera = MagicMock()
    fake_camera.rtsps_streams = _fake_rtsp_streams(None)
    _with_bootstrap_camera(mock_client, "cam1", fake_camera)

    stt_provider = MagicMock()
    stt_provider.transcribe = AsyncMock(return_value="")

    bridge = UnifiBridge(
        config=UnifiConfig(host="127.0.0.1", port=443, api_key="k"),
        session_store=JSONFileSessionStore(tmp_path),
        client=mock_client,
        stt_provider=stt_provider,
    )

    decision = asyncio.run(bridge.handle_event(_event()))

    assert decision is not None
    assert "can't help" in decision.text.lower()


def test_unifi_config_rtsp_defaults() -> None:
    config = UnifiConfig()
    assert config.rtsp_quality == "high"
    assert config.listen_seconds == 10.0


def test_unifi_config_rtsp_from_env(monkeypatch) -> None:
    monkeypatch.setenv("KNOCK_UNIFI_RTSP_QUALITY", "package")
    monkeypatch.setenv("KNOCK_UNIFI_LISTEN_SECONDS", "8.5")

    config = UnifiConfig.from_env()

    assert config.rtsp_quality == "package"
    assert config.listen_seconds == 8.5
