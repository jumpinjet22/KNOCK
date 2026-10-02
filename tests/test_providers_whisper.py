import asyncio

from wyoming.asr import Transcript
from wyoming.event import async_read_event, async_write_event

from knock.config import WhisperConfig
from knock.providers.stt.whisper import WhisperSTTProvider


def _make_fake_whisper_handler(received_types: list[str]):
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while True:
            event = await async_read_event(reader)
            if event is None:
                break
            received_types.append(event.type)
            if event.type == "audio-stop":
                break

        await async_write_event(Transcript(text="hello world").event(), writer)
        writer.close()
        await writer.wait_closed()

    return handler


async def _run_against_fake_server(audio: bytes, received_types: list[str]) -> str:
    server = await asyncio.start_server(_make_fake_whisper_handler(received_types), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    async with server:
        provider = WhisperSTTProvider(config=WhisperConfig(host="127.0.0.1", port=port))
        return await provider.transcribe(audio, rate=16000, width=2, channels=1)


def test_transcribe_returns_server_transcript() -> None:
    text = asyncio.run(_run_against_fake_server(b"\x00\x00" * 4000, []))
    assert text == "hello world"


def test_transcribe_sends_full_wyoming_sequence() -> None:
    received_types: list[str] = []
    asyncio.run(_run_against_fake_server(b"\x00\x00" * 4000, received_types))

    assert received_types[0] == "transcribe"
    assert "audio-start" in received_types
    assert "audio-chunk" in received_types
    assert received_types[-1] == "audio-stop"
