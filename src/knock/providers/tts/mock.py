from knock.providers.tts.base import SynthesizedAudio


class MockTTSProvider:
    name = "mock-tts"

    async def synthesize(self, text: str) -> SynthesizedAudio:
        return SynthesizedAudio(audio=text.encode(), rate=16000, width=2, channels=1)
