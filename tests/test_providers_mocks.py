import asyncio

from knock.providers.llm.mock import MockLLMProvider
from knock.providers.stt.mock import MockSTTProvider
from knock.providers.tts.mock import MockTTSProvider


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
