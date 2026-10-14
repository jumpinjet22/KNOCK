"""`from_env()` coverage for config fields added alongside Ollama tool-calling
-- `*Config.from_sources()`'s own precedence tests live in
test_config_from_sources.py.
"""

import pytest

from knock.config import (
    KnockConfig,
    OllamaConfig,
    UnifiConfig,
    VisionConfig,
    keep_alive_payload,
)


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


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, None),
        ("", None),
        ("  ", None),
        ("-1", -1),
        ("0", 0),
        ("300", 300),
        ("1.5", 1.5),
        ("10m", "10m"),
        (" 24h ", "24h"),
    ],
)
def test_keep_alive_payload(raw, expected) -> None:
    assert keep_alive_payload(raw) == expected


def test_safety_check_config_is_none_when_disabled() -> None:
    assert OllamaConfig().safety_check_config() is None


def test_safety_check_config_defaults_to_the_main_server() -> None:
    config = OllamaConfig(host="10.0.0.5", port=11434, model="qwen3.5:9b", safety_check_model="s")

    safety = config.safety_check_config()

    assert safety is not None
    assert (safety.host, safety.port, safety.model) == ("10.0.0.5", 11434, "s")


def test_safety_check_config_can_target_its_own_server() -> None:
    config = OllamaConfig(
        host="10.0.0.5",
        model="qwen3.5:9b",
        safety_check_model="qwen3:1.7b",
        safety_check_host="10.0.0.6",
        safety_check_port=11435,
    )

    safety = config.safety_check_config()

    assert safety is not None
    assert (safety.host, safety.port, safety.model) == ("10.0.0.6", 11435, "qwen3:1.7b")
    # The main config is untouched.
    assert (config.host, config.model) == ("10.0.0.5", "qwen3.5:9b")


def test_ollama_config_from_env_reads_new_fields(monkeypatch) -> None:
    monkeypatch.setenv("KNOCK_OLLAMA_SAFETY_CHECK_HOST", "10.0.0.6")
    monkeypatch.setenv("KNOCK_OLLAMA_SAFETY_CHECK_PORT", "11435")
    monkeypatch.setenv("KNOCK_OLLAMA_KEEP_ALIVE", "-1")

    config = OllamaConfig.from_env()

    assert config.safety_check_host == "10.0.0.6"
    assert config.safety_check_port == 11435
    assert config.keep_alive == "-1"


def test_ollama_config_from_env_new_fields_default_unset(monkeypatch) -> None:
    for name in ("SAFETY_CHECK_HOST", "SAFETY_CHECK_PORT", "KEEP_ALIVE"):
        monkeypatch.delenv(f"KNOCK_OLLAMA_{name}", raising=False)

    config = OllamaConfig.from_env()

    assert config.safety_check_host is None
    assert config.safety_check_port is None
    assert config.keep_alive is None


def test_vision_config_from_env_reads_new_fields(monkeypatch) -> None:
    monkeypatch.setenv("KNOCK_VISION_KEEP_ALIVE", "24h")
    monkeypatch.setenv("KNOCK_VISION_SEND_IMAGE_TO_BRAIN", "true")

    config = VisionConfig.from_env()

    assert config.keep_alive == "24h"
    assert config.send_image_to_brain is True


def test_vision_config_send_image_to_brain_defaults_off(monkeypatch) -> None:
    monkeypatch.delenv("KNOCK_VISION_SEND_IMAGE_TO_BRAIN", raising=False)

    assert VisionConfig.from_env().send_image_to_brain is False


def test_unifi_config_end_silence_seconds(monkeypatch) -> None:
    monkeypatch.delenv("KNOCK_UNIFI_END_SILENCE_SECONDS", raising=False)
    assert UnifiConfig.from_env().end_silence_seconds == 1.0

    monkeypatch.setenv("KNOCK_UNIFI_END_SILENCE_SECONDS", "0")
    assert UnifiConfig.from_env().end_silence_seconds == 0.0
