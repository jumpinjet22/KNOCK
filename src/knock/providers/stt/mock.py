class MockSTTProvider:
    name = "mock-stt"

    async def transcribe(
        self, audio: bytes, *, rate: int = 16000, width: int = 2, channels: int = 1
    ) -> str:
        return f"MOCK_TRANSCRIPT:{len(audio)}bytes"
