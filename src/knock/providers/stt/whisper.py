from __future__ import annotations

from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.client import AsyncTcpClient

from knock.config import WhisperConfig

_CHUNK_SAMPLES = 1024


class WhisperSTTProvider:
    """Speech-to-text provider backed by a Wyoming protocol server.

    Compatible with e.g. `wyoming-faster-whisper`, as commonly run alongside
    Home Assistant's local voice pipeline.
    """

    name = "whisper-wyoming"

    def __init__(self, config: WhisperConfig | None = None) -> None:
        self.config = config or WhisperConfig()

    async def transcribe(
        self, audio: bytes, *, rate: int = 16000, width: int = 2, channels: int = 1
    ) -> str:
        chunk_bytes = _CHUNK_SAMPLES * width * channels

        async with AsyncTcpClient(
            self.config.host,
            self.config.port,
            connect_timeout=self.config.timeout,
            read_timeout=self.config.timeout,
        ) as client:
            await client.write_event(Transcribe().event())
            await client.write_event(AudioStart(rate=rate, width=width, channels=channels).event())

            for offset in range(0, len(audio), chunk_bytes):
                chunk = audio[offset : offset + chunk_bytes]
                await client.write_event(
                    AudioChunk(rate=rate, width=width, channels=channels, audio=chunk).event()
                )

            await client.write_event(AudioStop().event())

            while True:
                event = await client.read_event()
                if event is None:
                    raise ConnectionError(
                        "Wyoming STT server closed the connection before sending a transcript"
                    )
                if Transcript.is_type(event.type):
                    return Transcript.from_event(event).text
