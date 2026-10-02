import os

from pydantic import BaseModel, Field


class KnockConfig(BaseModel):
    """Application configuration with safe defaults."""

    app_name: str = "KNOCK"
    short_response_limit: int = Field(default=140, ge=20, le=500)


class OllamaConfig(BaseModel):
    """Connection settings for a local Ollama LLM server."""

    host: str = "127.0.0.1"
    port: int = 11434
    model: str = "llama3.2"
    timeout: float = 30.0

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @classmethod
    def from_env(cls) -> "OllamaConfig":
        return cls(
            host=os.environ.get("KNOCK_OLLAMA_HOST", "127.0.0.1"),
            port=int(os.environ.get("KNOCK_OLLAMA_PORT", "11434")),
            model=os.environ.get("KNOCK_OLLAMA_MODEL", "llama3.2"),
            timeout=float(os.environ.get("KNOCK_OLLAMA_TIMEOUT", "30.0")),
        )


class WhisperConfig(BaseModel):
    """Connection settings for a Wyoming-protocol Whisper STT server."""

    host: str = "127.0.0.1"
    port: int = 10300
    timeout: float = 10.0

    @classmethod
    def from_env(cls) -> "WhisperConfig":
        return cls(
            host=os.environ.get("KNOCK_WHISPER_HOST", "127.0.0.1"),
            port=int(os.environ.get("KNOCK_WHISPER_PORT", "10300")),
            timeout=float(os.environ.get("KNOCK_WHISPER_TIMEOUT", "10.0")),
        )


class KokoroConfig(BaseModel):
    """Connection settings for a Wyoming-protocol Kokoro TTS server."""

    host: str = "127.0.0.1"
    port: int = 10200
    voice: str | None = None
    timeout: float = 10.0

    @classmethod
    def from_env(cls) -> "KokoroConfig":
        return cls(
            host=os.environ.get("KNOCK_KOKORO_HOST", "127.0.0.1"),
            port=int(os.environ.get("KNOCK_KOKORO_PORT", "10200")),
            voice=os.environ.get("KNOCK_KOKORO_VOICE"),
            timeout=float(os.environ.get("KNOCK_KOKORO_TIMEOUT", "10.0")),
        )


class MqttConfig(BaseModel):
    """Connection settings for the MQTT visitor-event bridge."""

    host: str = "127.0.0.1"
    port: int = 1883
    client_id: str = "knock"
    topic_in: str = "knock/events"
    topic_out: str = "knock/responses"
    username: str | None = None
    password: str | None = None
    keepalive: int = 60

    @classmethod
    def from_env(cls) -> "MqttConfig":
        return cls(
            host=os.environ.get("KNOCK_MQTT_HOST", "127.0.0.1"),
            port=int(os.environ.get("KNOCK_MQTT_PORT", "1883")),
            client_id=os.environ.get("KNOCK_MQTT_CLIENT_ID", "knock"),
            topic_in=os.environ.get("KNOCK_MQTT_TOPIC_IN", "knock/events"),
            topic_out=os.environ.get("KNOCK_MQTT_TOPIC_OUT", "knock/responses"),
            username=os.environ.get("KNOCK_MQTT_USERNAME"),
            password=os.environ.get("KNOCK_MQTT_PASSWORD"),
            keepalive=int(os.environ.get("KNOCK_MQTT_KEEPALIVE", "60")),
        )


class VisionConfig(BaseModel):
    """Connection settings for the Ollama-backed vision provider."""

    host: str = "127.0.0.1"
    port: int = 11434
    model: str = "moondream"
    timeout: float = 30.0
    prompt: str = (
        "Describe only the literal, visible objects and people in this image "
        "in one short, neutral sentence (for example: a person, a package, a "
        "box, a vehicle). Do not guess what any object contains, and do not "
        "speculate about danger, intent, or identity. Do not mention "
        "weapons, explosives, or threats unless unambiguously and clearly "
        "visible. If uncertain, describe only the general shape or type of "
        "object you can plainly see."
    )

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @classmethod
    def from_env(cls) -> "VisionConfig":
        return cls(
            host=os.environ.get("KNOCK_VISION_HOST", "127.0.0.1"),
            port=int(os.environ.get("KNOCK_VISION_PORT", "11434")),
            model=os.environ.get("KNOCK_VISION_MODEL", "moondream"),
            timeout=float(os.environ.get("KNOCK_VISION_TIMEOUT", "30.0")),
            prompt=os.environ.get("KNOCK_VISION_PROMPT", cls.model_fields["prompt"].default),
        )


class FrigateConfig(BaseModel):
    """Connection settings for the Frigate NVR bridge."""

    mqtt_host: str = "127.0.0.1"
    mqtt_port: int = 1883
    topic_prefix: str = "frigate"
    topic_out: str = "knock/responses"
    client_id: str = "knock-frigate"
    username: str | None = None
    password: str | None = None
    keepalive: int = 60
    http_host: str = "127.0.0.1"
    http_port: int = 5000
    trigger_labels: list[str] = Field(default_factory=lambda: ["person"])
    zones: list[str] = Field(default_factory=list)

    @classmethod
    def from_env(cls) -> "FrigateConfig":
        def _csv(value: str | None) -> list[str] | None:
            if value is None:
                return None
            return [item.strip() for item in value.split(",") if item.strip()]

        trigger_labels = _csv(os.environ.get("KNOCK_FRIGATE_TRIGGER_LABELS"))
        zones = _csv(os.environ.get("KNOCK_FRIGATE_ZONES"))

        return cls(
            mqtt_host=os.environ.get("KNOCK_FRIGATE_MQTT_HOST", "127.0.0.1"),
            mqtt_port=int(os.environ.get("KNOCK_FRIGATE_MQTT_PORT", "1883")),
            topic_prefix=os.environ.get("KNOCK_FRIGATE_TOPIC_PREFIX", "frigate"),
            topic_out=os.environ.get("KNOCK_FRIGATE_TOPIC_OUT", "knock/responses"),
            client_id=os.environ.get("KNOCK_FRIGATE_CLIENT_ID", "knock-frigate"),
            username=os.environ.get("KNOCK_FRIGATE_USERNAME"),
            password=os.environ.get("KNOCK_FRIGATE_PASSWORD"),
            keepalive=int(os.environ.get("KNOCK_FRIGATE_KEEPALIVE", "60")),
            http_host=os.environ.get("KNOCK_FRIGATE_HTTP_HOST", "127.0.0.1"),
            http_port=int(os.environ.get("KNOCK_FRIGATE_HTTP_PORT", "5000")),
            trigger_labels=trigger_labels if trigger_labels is not None else ["person"],
            zones=zones if zones is not None else [],
        )


class HomeAssistantConfig(BaseModel):
    """Connection settings for the Home Assistant bridge."""

    base_url: str = "http://homeassistant.local:8123"
    token: str = ""
    trigger_entity_id: str = "binary_sensor.front_doorbell"
    notify_service: str | None = None
    verify_ssl: bool = True

    @classmethod
    def from_env(cls) -> "HomeAssistantConfig":
        verify_ssl_raw = os.environ.get("KNOCK_HA_VERIFY_SSL", "true").strip().lower()
        return cls(
            base_url=os.environ.get("KNOCK_HA_BASE_URL", "http://homeassistant.local:8123"),
            token=os.environ.get("KNOCK_HA_TOKEN", ""),
            trigger_entity_id=os.environ.get(
                "KNOCK_HA_TRIGGER_ENTITY_ID", "binary_sensor.front_doorbell"
            ),
            notify_service=os.environ.get("KNOCK_HA_NOTIFY_SERVICE"),
            verify_ssl=verify_ssl_raw not in ("false", "0", "no"),
        )


class UnifiConfig(BaseModel):
    """Connection settings for the UniFi Protect bridge."""

    host: str = "127.0.0.1"
    port: int = 443
    api_key: str = ""
    verify_ssl: bool = False
    trigger_on: list[str] = Field(default_factory=lambda: ["ring"])
    rtsp_quality: str = "high"
    listen_seconds: float = 6.0

    @classmethod
    def from_env(cls) -> "UnifiConfig":
        trigger_on_raw = os.environ.get("KNOCK_UNIFI_TRIGGER_ON")
        trigger_on = (
            [item.strip() for item in trigger_on_raw.split(",") if item.strip()]
            if trigger_on_raw is not None
            else ["ring"]
        )
        verify_ssl_raw = os.environ.get("KNOCK_UNIFI_VERIFY_SSL", "false").strip().lower()

        return cls(
            host=os.environ.get("KNOCK_UNIFI_HOST", "127.0.0.1"),
            port=int(os.environ.get("KNOCK_UNIFI_PORT", "443")),
            api_key=os.environ.get("KNOCK_UNIFI_API_KEY", ""),
            verify_ssl=verify_ssl_raw not in ("false", "0", "no"),
            trigger_on=trigger_on,
            rtsp_quality=os.environ.get("KNOCK_UNIFI_RTSP_QUALITY", "high"),
            listen_seconds=float(os.environ.get("KNOCK_UNIFI_LISTEN_SECONDS", "6.0")),
        )
