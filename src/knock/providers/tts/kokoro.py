from __future__ import annotations

from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.client import AsyncTcpClient
from wyoming.tts import Synthesize, SynthesizeVoice

from knock.config import KokoroConfig
from knock.providers.tts.base import SynthesizedAudio


class KokoroTTSProvider:
    """Text-to-speech provider backed by a Wyoming protocol server.

    Compatible with e.g. a `wyoming`-wrapped Kokoro TTS server, as commonly run
    alongside Home Assistant's local voice pipeline.
    """

    name = "kokoro-wyoming"

    def __init__(self, config: KokoroConfig | None = None) -> None:
        self.config = config or KokoroConfig()

    async def synthesize(self, text: str) -> SynthesizedAudio:
        voice = SynthesizeVoice(name=self.config.voice) if self.config.voice else None

        audio_format: AudioStart | None = None
        chunks: list[bytes] = []

        async with AsyncTcpClient(
            self.config.host,
            self.config.port,
            connect_timeout=self.config.timeout,
            read_timeout=self.config.timeout,
        ) as client:
            await client.write_event(Synthesize(text=text, voice=voice).event())

            while True:
                event = await client.read_event()
                if event is None:
                    break
                if AudioStart.is_type(event.type):
                    audio_format = AudioStart.from_event(event)
                elif AudioChunk.is_type(event.type):
                    chunks.append(AudioChunk.from_event(event).audio)
                elif AudioStop.is_type(event.type):
                    break

        if audio_format is None:
            raise ConnectionError("Wyoming TTS server did not send an audio-start event")

        return SynthesizedAudio(
            audio=b"".join(chunks),
            rate=audio_format.rate,
            width=audio_format.width,
            channels=audio_format.channels,
        )
