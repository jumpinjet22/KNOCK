from typing import Protocol

from pydantic import BaseModel


class SynthesizedAudio(BaseModel):
    """Raw PCM audio returned by a TTS provider."""

    audio: bytes
    rate: int
    width: int
    channels: int


class TTSProvider(Protocol):
    name: str

    async def synthesize(self, text: str) -> SynthesizedAudio: ...
