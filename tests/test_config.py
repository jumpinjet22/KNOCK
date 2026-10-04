"""`from_env()` coverage for config fields added alongside Ollama tool-calling
-- `*Config.from_sources()`'s own precedence tests live in
test_config_from_sources.py.
"""

from knock.config import KnockConfig, OllamaConfig


def test_knock_config_from_env_defaults(monkeypatch) -> None:
    monkeypatch.delenv("KNOCK_PUBLIC_BASE_URL", raising=False)
    config = KnockConfig.from_env()
    assert config.public_base_url is None


def test_knock_config_from_env_reads_public_base_url(monkeypatch) -> None:
    monkeypatch.setenv("KNOCK_PUBLIC_BASE_URL", "https://knock.example.com")
    config = KnockConfig.from_env()
    assert config.public_base_url == "https://knock.example.com"


def test_ollama_config_from_env_use_tool_calling_defaults_to_none(monkeypatch) -> None:
    monkeypatch.delenv("KNOCK_OLLAMA_USE_TOOL_CALLING", raising=False)
    config = OllamaConfig.from_env()
    assert config.use_tool_calling is None


def test_ollama_config_from_env_use_tool_calling_true(monkeypatch) -> None:
    monkeypatch.setenv("KNOCK_OLLAMA_USE_TOOL_CALLING", "true")
    config = OllamaConfig.from_env()
    assert config.use_tool_calling is True


def test_ollama_config_from_env_use_tool_calling_false(monkeypatch) -> None:
    monkeypatch.setenv("KNOCK_OLLAMA_USE_TOOL_CALLING", "false")
    config = OllamaConfig.from_env()
    assert config.use_tool_calling is False


def test_ollama_config_from_env_confidence_thresholds_default(monkeypatch) -> None:
    monkeypatch.delenv("KNOCK_OLLAMA_INTENT_REVIEW_CONFIDENCE_FLOOR", raising=False)
    monkeypatch.delenv("KNOCK_OLLAMA_INTENT_REVIEW_CONFIDENCE_MARGIN", raising=False)
    config = OllamaConfig.from_env()
    assert config.intent_review_confidence_floor == 0.5
    assert config.intent_review_confidence_margin == 0.15
