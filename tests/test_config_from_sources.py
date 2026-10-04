"""Covers `*Config.from_sources()` -- the env var > ConfigStore > default
precedence that backs the web UI's settings pages. `from_env()` (env var >
default only) already has its own tests per integration/provider test file;
this file is specifically about the new middle layer and the ordering
between all three.
"""

from knock.config import (
    FrigateConfig,
    HomeAssistantConfig,
    KokoroConfig,
    MqttConfig,
    OllamaConfig,
    UnifiConfig,
    VisionConfig,
    WhisperConfig,
)
from knock.core.config_store import ConfigStore


def _store(tmp_path) -> ConfigStore:
    return ConfigStore(tmp_path / "config.json")


# -- Ollama -----------------------------------------------------------------


def test_ollama_from_sources_uses_defaults_when_nothing_set(tmp_path) -> None:
    config = OllamaConfig.from_sources(_store(tmp_path))
    assert config.host == "127.0.0.1"
    assert config.model == "llama3.2"


def test_ollama_from_sources_uses_stored_values(tmp_path) -> None:
    store = _store(tmp_path)
    store.set_section("ollama", {"host": "192.168.1.54", "model": "qwen3.5:9b"})

    config = OllamaConfig.from_sources(store)

    assert config.host == "192.168.1.54"
    assert config.model == "qwen3.5:9b"


def test_ollama_from_sources_env_var_wins_over_store(tmp_path, monkeypatch) -> None:
    store = _store(tmp_path)
    store.set_section("ollama", {"host": "192.168.1.54"})
    monkeypatch.setenv("KNOCK_OLLAMA_HOST", "10.0.0.1")

    config = OllamaConfig.from_sources(store)

    assert config.host == "10.0.0.1"


def test_ollama_use_tool_calling_defaults_to_none_for_auto_detect(tmp_path) -> None:
    config = OllamaConfig.from_sources(_store(tmp_path))
    assert config.use_tool_calling is None


def test_ollama_use_tool_calling_reads_an_explicit_false_from_the_store(tmp_path) -> None:
    store = _store(tmp_path)
    store.set_section("ollama", {"use_tool_calling": False})

    config = OllamaConfig.from_sources(store)

    assert config.use_tool_calling is False


def test_ollama_use_tool_calling_env_var_overrides_a_stored_value(tmp_path, monkeypatch) -> None:
    store = _store(tmp_path)
    store.set_section("ollama", {"use_tool_calling": False})
    monkeypatch.setenv("KNOCK_OLLAMA_USE_TOOL_CALLING", "true")

    config = OllamaConfig.from_sources(store)

    assert config.use_tool_calling is True


# -- Vision -------------------------------------------------------------------


def test_vision_from_sources_uses_stored_model_and_prompt(tmp_path) -> None:
    store = _store(tmp_path)
    store.set_section("vision", {"model": "llava", "prompt": "Describe briefly."})

    config = VisionConfig.from_sources(store)

    assert config.model == "llava"
    assert config.prompt == "Describe briefly."


def test_vision_from_sources_falls_back_to_default_prompt(tmp_path) -> None:
    config = VisionConfig.from_sources(_store(tmp_path))
    assert "literal, visible objects" in config.prompt


# -- Whisper / Kokoro -----------------------------------------------------------


def test_whisper_from_sources_uses_stored_values(tmp_path) -> None:
    store = _store(tmp_path)
    store.set_section("whisper", {"host": "192.168.1.2", "port": 10301})

    config = WhisperConfig.from_sources(store)

    assert config.host == "192.168.1.2"
    assert config.port == 10301


def test_kokoro_from_sources_uses_stored_voice(tmp_path) -> None:
    store = _store(tmp_path)
    store.set_section("kokoro", {"voice": "af_bella"})

    config = KokoroConfig.from_sources(store)

    assert config.voice == "af_bella"


def test_kokoro_from_sources_env_var_wins_over_store(tmp_path, monkeypatch) -> None:
    store = _store(tmp_path)
    store.set_section("kokoro", {"voice": "af_bella"})
    monkeypatch.setenv("KNOCK_KOKORO_VOICE", "am_adam")

    config = KokoroConfig.from_sources(store)

    assert config.voice == "am_adam"


# -- Mqtt -----------------------------------------------------------------------


def test_mqtt_from_sources_uses_stored_values_including_secret(tmp_path) -> None:
    store = _store(tmp_path)
    store.set_section("mqtt", {"host": "broker.local", "username": "knock", "password": "hunter2"})

    config = MqttConfig.from_sources(store)

    assert config.host == "broker.local"
    assert config.username == "knock"
    assert config.password is not None
    assert config.password.get_secret_value() == "hunter2"


def test_mqtt_from_sources_env_var_wins_for_password(tmp_path, monkeypatch) -> None:
    store = _store(tmp_path)
    store.set_section("mqtt", {"password": "stored-password"})
    monkeypatch.setenv("KNOCK_MQTT_PASSWORD", "env-password")

    config = MqttConfig.from_sources(store)

    assert config.password is not None
    assert config.password.get_secret_value() == "env-password"


# -- Frigate --------------------------------------------------------------------


def test_frigate_from_sources_uses_stored_lists(tmp_path) -> None:
    store = _store(tmp_path)
    store.set_section(
        "frigate", {"trigger_labels": ["person", "package"], "zones": ["front_porch"]}
    )

    config = FrigateConfig.from_sources(store)

    assert config.trigger_labels == ["person", "package"]
    assert config.zones == ["front_porch"]


def test_frigate_from_sources_env_var_wins_for_trigger_labels(tmp_path, monkeypatch) -> None:
    store = _store(tmp_path)
    store.set_section("frigate", {"trigger_labels": ["person"]})
    monkeypatch.setenv("KNOCK_FRIGATE_TRIGGER_LABELS", "car,package")

    config = FrigateConfig.from_sources(store)

    assert config.trigger_labels == ["car", "package"]


# -- Home Assistant ---------------------------------------------------------------


def test_homeassistant_from_sources_uses_stored_token(tmp_path) -> None:
    store = _store(tmp_path)
    store.set_section(
        "homeassistant", {"base_url": "http://ha.local:8123", "token": "secret-token"}
    )

    config = HomeAssistantConfig.from_sources(store)

    assert config.base_url == "http://ha.local:8123"
    assert config.token.get_secret_value() == "secret-token"


def test_homeassistant_from_sources_stored_verify_ssl_as_real_bool(tmp_path) -> None:
    store = _store(tmp_path)
    store.set_section("homeassistant", {"verify_ssl": False})

    config = HomeAssistantConfig.from_sources(store)

    assert config.verify_ssl is False


def test_homeassistant_from_sources_env_var_wins_for_token(tmp_path, monkeypatch) -> None:
    store = _store(tmp_path)
    store.set_section("homeassistant", {"token": "stored-token"})
    monkeypatch.setenv("KNOCK_HA_TOKEN", "env-token")

    config = HomeAssistantConfig.from_sources(store)

    assert config.token.get_secret_value() == "env-token"


# -- UniFi Protect ----------------------------------------------------------------


def test_unifi_from_sources_uses_stored_api_key_and_trigger_on(tmp_path) -> None:
    store = _store(tmp_path)
    store.set_section(
        "unifi", {"host": "192.168.1.1", "api_key": "secret-key", "trigger_on": ["ring", "person"]}
    )

    config = UnifiConfig.from_sources(store)

    assert config.host == "192.168.1.1"
    assert config.api_key.get_secret_value() == "secret-key"
    assert config.trigger_on == ["ring", "person"]


def test_unifi_from_sources_env_var_wins_for_api_key(tmp_path, monkeypatch) -> None:
    store = _store(tmp_path)
    store.set_section("unifi", {"api_key": "stored-key"})
    monkeypatch.setenv("KNOCK_UNIFI_API_KEY", "env-key")

    config = UnifiConfig.from_sources(store)

    assert config.api_key.get_secret_value() == "env-key"


def test_unifi_from_sources_falls_back_to_default_when_unset(tmp_path) -> None:
    config = UnifiConfig.from_sources(_store(tmp_path))
    assert config.rtsp_quality == "high"
    assert config.trigger_on == ["ring"]
    assert config.trigger_camera_ids == []


def test_unifi_from_sources_uses_stored_trigger_camera_ids(tmp_path) -> None:
    store = _store(tmp_path)
    store.set_section("unifi", {"trigger_camera_ids": ["doorbell-cam"]})

    config = UnifiConfig.from_sources(store)

    assert config.trigger_camera_ids == ["doorbell-cam"]


def test_unifi_from_sources_env_var_wins_for_trigger_camera_ids(tmp_path, monkeypatch) -> None:
    store = _store(tmp_path)
    store.set_section("unifi", {"trigger_camera_ids": ["stored-cam"]})
    monkeypatch.setenv("KNOCK_UNIFI_TRIGGER_CAMERA_IDS", "env-cam")

    config = UnifiConfig.from_sources(store)

    assert config.trigger_camera_ids == ["env-cam"]
