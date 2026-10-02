import asyncio
import base64
import io
import threading
import wave

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from wyoming.asr import Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.event import async_read_event, async_write_event

from knock.api.app import app
from knock.api.auth_routes import get_auth_store, get_web_session_store
from knock.api.settings_routes import get_config_store
from knock.config import OllamaConfig, VisionConfig
from knock.core.auth import AuthStore, WebSessionStore
from knock.core.config_store import ConfigStore


@pytest.fixture
def config_store(tmp_path) -> ConfigStore:
    return ConfigStore(tmp_path / "config.json")


@pytest.fixture
def client(tmp_path, config_store):
    app.dependency_overrides[get_auth_store] = lambda: AuthStore(tmp_path / "auth.json")
    app.dependency_overrides[get_web_session_store] = lambda: WebSessionStore(
        tmp_path / "web_sessions.json"
    )
    app.dependency_overrides[get_config_store] = lambda: config_store
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _post(client: TestClient, url: str, json: dict):
    headers = {}
    token = client.cookies.get("csrftoken")
    if token:
        headers["x-csrftoken"] = token
    return client.post(url, json=json, headers=headers)


def _login(client: TestClient) -> None:
    resp = _post(client, "/api/auth/setup", {"username": "jon", "password": "a-good-password"})
    assert resp.status_code == 201


def _wav_base64(pcm: bytes, *, rate: int = 16000, width: int = 2, channels: int = 1) -> str:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(width)
        wav_file.setframerate(rate)
        wav_file.writeframes(pcm)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


class _BackgroundServer:
    """Runs an `asyncio.start_server` handler on a background thread.

    The Wyoming providers (whisper/kokoro) connect over a real TCP socket,
    so a fake server needs to actually be listening -- this runs it on its
    own thread/event loop so the (sync) `TestClient` call in the test body
    can talk to it over localhost without needing to share an event loop.
    """

    def __init__(self, handler) -> None:
        self._handler = handler
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: asyncio.Server | None = None
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self.port = 0

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop

        async def serve() -> None:
            server = await asyncio.start_server(self._handler, "127.0.0.1", 0)
            self._server = server
            self.port = server.sockets[0].getsockname()[1]
            self._ready.set()
            async with server:
                await server.serve_forever()

        try:
            loop.run_until_complete(serve())
        except asyncio.CancelledError:
            pass

    def __enter__(self) -> "_BackgroundServer":
        self._thread.start()
        self._ready.wait(timeout=2)
        return self

    def __exit__(self, *exc_info: object) -> None:
        assert self._loop is not None and self._server is not None
        self._loop.call_soon_threadsafe(self._server.close)
        self._thread.join(timeout=2)


# -- auth gating ----------------------------------------------------------------


@pytest.mark.parametrize(
    "method,url",
    [
        ("post", "/api/debug/vision/describe"),
        ("post", "/api/debug/llm/generate"),
        ("post", "/api/debug/stt/transcribe"),
        ("post", "/api/debug/tts/synthesize"),
        ("post", "/api/debug/conversation/simulate"),
    ],
)
def test_debug_routes_require_authentication(client, method, url) -> None:
    resp = getattr(client, method)(url, json={})
    assert resp.status_code == 401


# -- vision -----------------------------------------------------------------------


@respx.mock
def test_debug_vision_describe_returns_raw_and_sanitized(client, config_store) -> None:
    _login(client)
    config = VisionConfig.from_sources(config_store)
    respx.post(f"{config.base_url}/api/generate").mock(
        return_value=httpx.Response(200, json={"response": "A person with a box."})
    )

    resp = _post(
        client,
        "/api/debug/vision/describe",
        {"image_base64": base64.b64encode(b"fake-jpeg").decode("ascii")},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["raw"] == "A person with a box."
    assert body["sanitized"] == "A person with a box."
    assert body["alarming_language_detected"] is False
    assert body["latency_ms"] >= 0


@respx.mock
def test_debug_vision_describe_flags_alarming_raw_output(client, config_store) -> None:
    _login(client)
    config = VisionConfig.from_sources(config_store)
    respx.post(f"{config.base_url}/api/generate").mock(
        return_value=httpx.Response(200, json={"response": "possibly holding a knife"})
    )

    resp = _post(
        client,
        "/api/debug/vision/describe",
        {"image_base64": base64.b64encode(b"fake-jpeg").decode("ascii")},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["raw"] == "possibly holding a knife"
    assert body["sanitized"] != body["raw"]
    assert body["alarming_language_detected"] is True


def test_debug_vision_describe_rejects_bad_base64(client) -> None:
    _login(client)
    resp = _post(client, "/api/debug/vision/describe", {"image_base64": "not-valid-base64!!"})
    assert resp.status_code == 400


@respx.mock
def test_debug_vision_describe_reports_upstream_failure_as_502(client, config_store) -> None:
    _login(client)
    config = VisionConfig.from_sources(config_store)
    respx.post(f"{config.base_url}/api/generate").mock(return_value=httpx.Response(500))

    resp = _post(
        client,
        "/api/debug/vision/describe",
        {"image_base64": base64.b64encode(b"img").decode("ascii")},
    )

    assert resp.status_code == 502


@respx.mock
def test_debug_vision_describe_surfaces_the_server_error_body(client, config_store) -> None:
    _login(client)
    config = VisionConfig.from_sources(config_store)
    respx.post(f"{config.base_url}/api/generate").mock(
        return_value=httpx.Response(
            400, json={"error": "request exceeds the available context size"}
        )
    )

    resp = _post(
        client,
        "/api/debug/vision/describe",
        {"image_base64": base64.b64encode(b"img").decode("ascii")},
    )

    assert resp.status_code == 502
    assert "exceeds the available context size" in resp.json()["detail"]


# -- llm ----------------------------------------------------------------------------


@respx.mock
def test_debug_llm_generate_returns_response_text(client, config_store) -> None:
    _login(client)
    config = OllamaConfig.from_sources(config_store)
    respx.post(f"{config.base_url}/api/generate").mock(
        return_value=httpx.Response(200, json={"response": "Hello there"})
    )

    resp = _post(client, "/api/debug/llm/generate", {"prompt": "hi"})

    assert resp.status_code == 200
    assert resp.json()["response"] == "Hello there"


# -- stt ----------------------------------------------------------------------------


def test_debug_stt_transcribe_returns_transcript(client, config_store) -> None:
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while True:
            event = await async_read_event(reader)
            if event is None or event.type == "audio-stop":
                break
        await async_write_event(Transcript(text="hello world").event(), writer)
        writer.close()
        await writer.wait_closed()

    with _BackgroundServer(handler) as server:
        config_store.update_section("whisper", {"host": "127.0.0.1", "port": server.port})
        _login(client)
        resp = _post(
            client,
            "/api/debug/stt/transcribe",
            {"audio_wav_base64": _wav_base64(b"\x00\x00" * 4000)},
        )

    assert resp.status_code == 200
    assert resp.json()["transcript"] == "hello world"


def test_debug_stt_transcribe_rejects_non_wav_audio(client) -> None:
    _login(client)
    resp = _post(
        client,
        "/api/debug/stt/transcribe",
        {"audio_wav_base64": base64.b64encode(b"not a wav file").decode("ascii")},
    )
    assert resp.status_code == 400


def test_debug_stt_transcribe_reports_connection_failure_as_502(client, config_store) -> None:
    config_store.update_section("whisper", {"host": "127.0.0.1", "port": 1})
    _login(client)
    resp = _post(
        client,
        "/api/debug/stt/transcribe",
        {"audio_wav_base64": _wav_base64(b"\x00\x00" * 10)},
    )
    assert resp.status_code == 502


# -- tts ----------------------------------------------------------------------------


def test_debug_tts_synthesize_returns_playable_wav(client, config_store) -> None:
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await async_read_event(reader)
        await async_write_event(AudioStart(rate=22050, width=2, channels=1).event(), writer)
        await async_write_event(
            AudioChunk(rate=22050, width=2, channels=1, audio=b"\x01\x02").event(), writer
        )
        await async_write_event(AudioStop().event(), writer)
        writer.close()
        await writer.wait_closed()

    with _BackgroundServer(handler) as server:
        config_store.update_section("kokoro", {"host": "127.0.0.1", "port": server.port})
        _login(client)
        resp = _post(client, "/api/debug/tts/synthesize", {"text": "please open"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["rate"] == 22050
    assert body["width"] == 2
    assert body["channels"] == 1

    wav_bytes = base64.b64decode(body["audio_wav_base64"])
    with wave.open(io.BytesIO(wav_bytes), "rb") as wav_file:
        assert wav_file.getframerate() == 22050
        assert wav_file.readframes(wav_file.getnframes()) == b"\x01\x02"


def test_debug_tts_synthesize_reports_connection_failure_as_502(client, config_store) -> None:
    config_store.update_section("kokoro", {"host": "127.0.0.1", "port": 1})
    _login(client)
    resp = _post(client, "/api/debug/tts/synthesize", {"text": "hi"})
    assert resp.status_code == 502


# -- conversation simulator -----------------------------------------------------------


def test_debug_conversation_simulate_normal_intent(client) -> None:
    _login(client)
    resp = _post(client, "/api/debug/conversation/simulate", {"text": "I have a package for you"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["intent"] == "delivery"
    assert body["policy_allowed"] is True
    assert body["decision"]["escalate"] is False


def test_debug_conversation_simulate_emergency_escalates(client) -> None:
    _login(client)
    resp = _post(client, "/api/debug/conversation/simulate", {"text": "there is a fire, help!"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["decision"]["escalate"] is True
    assert body["decision"]["reason"] == "emergency"
    assert len(body["matched_rule_ids"]) > 0
