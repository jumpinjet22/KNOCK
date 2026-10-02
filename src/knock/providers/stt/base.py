from typing import Protocol


class STTProvider(Protocol):
    name: str

    async def transcribe(
        self, audio: bytes, *, rate: int = 16000, width: int = 2, channels: int = 1
    ) -> str: ...
