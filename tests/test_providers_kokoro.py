import asyncio

from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.event import async_read_event, async_write_event
from wyoming.info import Attribution, Info, TtsProgram, TtsVoice
from wyoming.tts import Synthesize

from knock.config import KokoroConfig
from knock.providers.tts.kokoro import KokoroTTSProvider


def _make_fake_kokoro_handler(received_texts: list[str]):
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        event = await async_read_event(reader)
        assert event is not None
        received_texts.append(Synthesize.from_event(event).text)

        await async_write_event(AudioStart(rate=22050, width=2, channels=1).event(), writer)
        await async_write_event(
            AudioChunk(rate=22050, width=2, channels=1, audio=b"\x01\x02").event(), writer
        )
        await async_write_event(
            AudioChunk(rate=22050, width=2, channels=1, audio=b"\x03\x04").event(), writer
        )
        await async_write_event(AudioStop().event(), writer)
        writer.close()
        await writer.wait_closed()

    return handler


async def _run_against_fake_server(text: str, received_texts: list[str]):
    server = await asyncio.start_server(_make_fake_kokoro_handler(received_texts), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    async with server:
        provider = KokoroTTSProvider(config=KokoroConfig(host="127.0.0.1", port=port))
        return await provider.synthesize(text)


def test_synthesize_returns_reassembled_audio() -> None:
    result = asyncio.run(_run_against_fake_server("hello", []))

    assert result.audio == b"\x01\x02\x03\x04"
    assert result.rate == 22050
    assert result.width == 2
    assert result.channels == 1


def test_synthesize_sends_requested_text() -> None:
    received_texts: list[str] = []
    asyncio.run(_run_against_fake_server("please open", received_texts))

    assert received_texts == ["please open"]


def _attribution() -> Attribution:
    return Attribution(name="test", url="https://example.invalid")


async def _fake_describe_handler(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    event = await async_read_event(reader)
    assert event is not None and event.type == "describe"

    info = Info(
        tts=[
            TtsProgram(
                name="kokoro",
                attribution=_attribution(),
                installed=True,
                description=None,
                version=None,
                voices=[
                    TtsVoice(
                        name="af_bella",
                        attribution=_attribution(),
                        installed=True,
                        description=None,
                        version=None,
                        languages=["en"],
                    ),
                    TtsVoice(
                        name="am_adam",
                        attribution=_attribution(),
                        installed=True,
                        description=None,
                        version=None,
                        languages=["en"],
                    ),
                ],
            )
        ]
    )
    await async_write_event(info.event(), writer)
    writer.close()
    await writer.wait_closed()


def test_list_voices_returns_voice_names_from_info() -> None:
    async def run() -> list[str]:
        server = await asyncio.start_server(_fake_describe_handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async with server:
            provider = KokoroTTSProvider(config=KokoroConfig(host="127.0.0.1", port=port))
            return await provider.list_voices()

    voices = asyncio.run(run())

    assert voices == ["af_bella", "am_adam"]


def test_list_voices_raises_if_server_closes_without_info() -> None:
    async def _handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await async_read_event(reader)
        writer.close()
        await writer.wait_closed()

    async def run() -> list[str]:
        server = await asyncio.start_server(_handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async with server:
            provider = KokoroTTSProvider(config=KokoroConfig(host="127.0.0.1", port=port))
            return await provider.list_voices()

    try:
        asyncio.run(run())
        raised = False
    except ConnectionError:
        raised = True
    assert raised is True
