import asyncio

from knock.providers.llm.mock import MockLLMProvider
from knock.providers.stt.mock import MockSTTProvider
from knock.providers.tts.mock import MockTTSProvider
from knock.providers.vision.mock import MockVisionProvider


def test_mock_llm_provider_echoes_prompt_prefix() -> None:
    result = MockLLMProvider().generate("hello there, general")
    assert result.startswith("MOCK_RESPONSE:")
    assert "hello there" in result


def test_mock_stt_provider_reports_audio_length() -> None:
    text = asyncio.run(MockSTTProvider().transcribe(b"\x00" * 10))
    assert text == "MOCK_TRANSCRIPT:10bytes"


def test_mock_tts_provider_echoes_text_as_audio() -> None:
    result = asyncio.run(MockTTSProvider().synthesize("hi"))
    assert result.audio == b"hi"
    assert result.rate == 16000


def test_mock_vision_provider_reports_image_length() -> None:
    result = MockVisionProvider().describe(b"\x00" * 20)
    assert result == "MOCK_DESCRIPTION:20bytes"
