from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field, SecretStr

from knock.core.config_store import ConfigStore


def _resolve(
    env_var: str,
    section: dict[str, Any],
    key: str,
    default: Any,
    cast: Callable[[str], Any] = str,
) -> Any:
    """Three-layer precedence shared by every `*Config.from_sources()`: an
    explicit env var always wins (so nothing deployed via `.env`/systemd/
    compose needs to change), then a value persisted in the `ConfigStore`
    (what the web UI writes), then the hardcoded default.
    """
    raw = os.environ.get(env_var)
    if raw is not None:
        return cast(raw)
    if key in section and section[key] is not None:
        return section[key]
    return default


def _csv(value: str | None) -> list[str] | None:
    if value is None:
        return None
    return [item.strip() for item in value.split(",") if item.strip()]


def _bool(value: str) -> bool:
    return value.strip().lower() not in ("false", "0", "no")


class KnockConfig(BaseModel):
    """Application configuration with safe defaults."""

    app_name: str = "KNOCK"
    short_response_limit: int = Field(default=140, ge=20, le=500)
    # Externally-reachable base URL for KNOCK's own web API (e.g. behind a
    # reverse proxy or a tunnel) -- needed so a notification's photo
    # attachment URL (see unifi.py's _build_snapshot_url) is fetchable from
    # the household's phone, not just from localhost. None (the default)
    # means notifications go out without a photo attachment.
    public_base_url: str | None = None

    @classmethod
    def from_env(cls) -> KnockConfig:
        return cls(
            app_name=os.environ.get("KNOCK_APP_NAME", "KNOCK"),
            short_response_limit=int(os.environ.get("KNOCK_SHORT_RESPONSE_LIMIT", "140")),
            public_base_url=os.environ.get("KNOCK_PUBLIC_BASE_URL"),
        )

    @classmethod
    def from_sources(cls, store: ConfigStore) -> KnockConfig:
        s = store.get_section("knock")
        return cls(
            app_name=_resolve("KNOCK_APP_NAME", s, "app_name", "KNOCK"),
            short_response_limit=_resolve(
                "KNOCK_SHORT_RESPONSE_LIMIT", s, "short_response_limit", 140, int
            ),
            public_base_url=_resolve("KNOCK_PUBLIC_BASE_URL", s, "public_base_url", None),
        )


class OllamaConfig(BaseModel):
    """Connection settings for a local Ollama LLM server."""

    host: str = "127.0.0.1"
    port: int = 11434
    model: str = "llama3.2"
    timeout: float = 30.0
    # Tri-state, not a plain bool: None ("auto") means a bridge should
    # capability-detect at startup (see core/tool_calling_detection.py)
    # rather than trusting a hardcoded default -- not every local model
    # handles Ollama tool-calling reliably. An explicit True/False is a
    # sticky operator override that skips detection entirely; in
    # particular, an explicit False is never silently re-enabled by
    # detection on a later restart, only by the operator changing it back.
    use_tool_calling: bool | None = None
    # Below this top-candidate confidence, or when the top two candidates'
    # confidences are within this margin of each other, classify_intent's
    # tool-call result is treated as "not a clear single winner" and
    # flag_for_review fires (see Orchestrator._refine_unknown_intent_via_tools).
    intent_review_confidence_floor: float = 0.5
    intent_review_confidence_margin: float = 0.15

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @classmethod
    def from_env(cls) -> OllamaConfig:
        use_tool_calling_raw = os.environ.get("KNOCK_OLLAMA_USE_TOOL_CALLING")
        return cls(
            host=os.environ.get("KNOCK_OLLAMA_HOST", "127.0.0.1"),
            port=int(os.environ.get("KNOCK_OLLAMA_PORT", "11434")),
            model=os.environ.get("KNOCK_OLLAMA_MODEL", "llama3.2"),
            timeout=float(os.environ.get("KNOCK_OLLAMA_TIMEOUT", "30.0")),
            use_tool_calling=(
                _bool(use_tool_calling_raw) if use_tool_calling_raw is not None else None
            ),
            intent_review_confidence_floor=float(
                os.environ.get("KNOCK_OLLAMA_INTENT_REVIEW_CONFIDENCE_FLOOR", "0.5")
            ),
            intent_review_confidence_margin=float(
                os.environ.get("KNOCK_OLLAMA_INTENT_REVIEW_CONFIDENCE_MARGIN", "0.15")
            ),
        )

    @classmethod
    def from_sources(cls, store: ConfigStore) -> OllamaConfig:
        s = store.get_section("ollama")
        return cls(
            host=_resolve("KNOCK_OLLAMA_HOST", s, "host", "127.0.0.1"),
            port=_resolve("KNOCK_OLLAMA_PORT", s, "port", 11434, int),
            model=_resolve("KNOCK_OLLAMA_MODEL", s, "model", "llama3.2"),
            timeout=_resolve("KNOCK_OLLAMA_TIMEOUT", s, "timeout", 30.0, float),
            use_tool_calling=_resolve(
                "KNOCK_OLLAMA_USE_TOOL_CALLING", s, "use_tool_calling", None, _bool
            ),
            intent_review_confidence_floor=_resolve(
                "KNOCK_OLLAMA_INTENT_REVIEW_CONFIDENCE_FLOOR",
                s,
                "intent_review_confidence_floor",
                0.5,
                float,
            ),
            intent_review_confidence_margin=_resolve(
                "KNOCK_OLLAMA_INTENT_REVIEW_CONFIDENCE_MARGIN",
                s,
                "intent_review_confidence_margin",
                0.15,
                float,
            ),
        )


class WhisperConfig(BaseModel):
    """Connection settings for a Wyoming-protocol Whisper STT server."""

    host: str = "127.0.0.1"
    port: int = 10300
    timeout: float = 10.0

    @classmethod
    def from_env(cls) -> WhisperConfig:
        return cls(
            host=os.environ.get("KNOCK_WHISPER_HOST", "127.0.0.1"),
            port=int(os.environ.get("KNOCK_WHISPER_PORT", "10300")),
            timeout=float(os.environ.get("KNOCK_WHISPER_TIMEOUT", "10.0")),
        )

    @classmethod
    def from_sources(cls, store: ConfigStore) -> WhisperConfig:
        s = store.get_section("whisper")
        return cls(
            host=_resolve("KNOCK_WHISPER_HOST", s, "host", "127.0.0.1"),
            port=_resolve("KNOCK_WHISPER_PORT", s, "port", 10300, int),
            timeout=_resolve("KNOCK_WHISPER_TIMEOUT", s, "timeout", 10.0, float),
        )


class KokoroConfig(BaseModel):
    """Connection settings for a Wyoming-protocol Kokoro TTS server."""

    host: str = "127.0.0.1"
    port: int = 10200
    voice: str | None = None
    timeout: float = 10.0

    @classmethod
    def from_env(cls) -> KokoroConfig:
        return cls(
            host=os.environ.get("KNOCK_KOKORO_HOST", "127.0.0.1"),
            port=int(os.environ.get("KNOCK_KOKORO_PORT", "10200")),
            voice=os.environ.get("KNOCK_KOKORO_VOICE"),
            timeout=float(os.environ.get("KNOCK_KOKORO_TIMEOUT", "10.0")),
        )

    @classmethod
    def from_sources(cls, store: ConfigStore) -> KokoroConfig:
        s = store.get_section("kokoro")
        return cls(
            host=_resolve("KNOCK_KOKORO_HOST", s, "host", "127.0.0.1"),
            port=_resolve("KNOCK_KOKORO_PORT", s, "port", 10200, int),
            voice=_resolve("KNOCK_KOKORO_VOICE", s, "voice", None),
            timeout=_resolve("KNOCK_KOKORO_TIMEOUT", s, "timeout", 10.0, float),
        )


class MqttConfig(BaseModel):
    """Connection settings for the MQTT visitor-event bridge."""

    host: str = "127.0.0.1"
    port: int = 1883
    client_id: str = "knock"
    topic_in: str = "knock/events"
    topic_out: str = "knock/responses"
    username: str | None = None
    password: SecretStr | None = None
    keepalive: int = 60

    @classmethod
    def from_env(cls) -> MqttConfig:
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

    @classmethod
    def from_sources(cls, store: ConfigStore) -> MqttConfig:
        s = store.get_section("mqtt")
        return cls(
            host=_resolve("KNOCK_MQTT_HOST", s, "host", "127.0.0.1"),
            port=_resolve("KNOCK_MQTT_PORT", s, "port", 1883, int),
            client_id=_resolve("KNOCK_MQTT_CLIENT_ID", s, "client_id", "knock"),
            topic_in=_resolve("KNOCK_MQTT_TOPIC_IN", s, "topic_in", "knock/events"),
            topic_out=_resolve("KNOCK_MQTT_TOPIC_OUT", s, "topic_out", "knock/responses"),
            username=_resolve("KNOCK_MQTT_USERNAME", s, "username", None),
            password=_resolve("KNOCK_MQTT_PASSWORD", s, "password", None),
            keepalive=_resolve("KNOCK_MQTT_KEEPALIVE", s, "keepalive", 60, int),
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
    def from_env(cls) -> VisionConfig:
        return cls(
            host=os.environ.get("KNOCK_VISION_HOST", "127.0.0.1"),
            port=int(os.environ.get("KNOCK_VISION_PORT", "11434")),
            model=os.environ.get("KNOCK_VISION_MODEL", "moondream"),
            timeout=float(os.environ.get("KNOCK_VISION_TIMEOUT", "30.0")),
            prompt=os.environ.get("KNOCK_VISION_PROMPT", cls.model_fields["prompt"].default),
        )

    @classmethod
    def from_sources(cls, store: ConfigStore) -> VisionConfig:
        s = store.get_section("vision")
        return cls(
            host=_resolve("KNOCK_VISION_HOST", s, "host", "127.0.0.1"),
            port=_resolve("KNOCK_VISION_PORT", s, "port", 11434, int),
            model=_resolve("KNOCK_VISION_MODEL", s, "model", "moondream"),
            timeout=_resolve("KNOCK_VISION_TIMEOUT", s, "timeout", 30.0, float),
            prompt=_resolve("KNOCK_VISION_PROMPT", s, "prompt", cls.model_fields["prompt"].default),
        )


class FrigateConfig(BaseModel):
    """Connection settings for the Frigate NVR bridge."""

    mqtt_host: str = "127.0.0.1"
    mqtt_port: int = 1883
    topic_prefix: str = "frigate"
    topic_out: str = "knock/responses"
    client_id: str = "knock-frigate"
    username: str | None = None
    password: SecretStr | None = None
    keepalive: int = 60
    http_host: str = "127.0.0.1"
    http_port: int = 5000
    trigger_labels: list[str] = Field(default_factory=lambda: ["person"])
    zones: list[str] = Field(default_factory=list)

    @classmethod
    def from_env(cls) -> FrigateConfig:
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

    @classmethod
    def from_sources(cls, store: ConfigStore) -> FrigateConfig:
        s = store.get_section("frigate")
        trigger_labels_env = _csv(os.environ.get("KNOCK_FRIGATE_TRIGGER_LABELS"))
        zones_env = _csv(os.environ.get("KNOCK_FRIGATE_ZONES"))

        return cls(
            mqtt_host=_resolve("KNOCK_FRIGATE_MQTT_HOST", s, "mqtt_host", "127.0.0.1"),
            mqtt_port=_resolve("KNOCK_FRIGATE_MQTT_PORT", s, "mqtt_port", 1883, int),
            topic_prefix=_resolve("KNOCK_FRIGATE_TOPIC_PREFIX", s, "topic_prefix", "frigate"),
            topic_out=_resolve("KNOCK_FRIGATE_TOPIC_OUT", s, "topic_out", "knock/responses"),
            client_id=_resolve("KNOCK_FRIGATE_CLIENT_ID", s, "client_id", "knock-frigate"),
            username=_resolve("KNOCK_FRIGATE_USERNAME", s, "username", None),
            password=_resolve("KNOCK_FRIGATE_PASSWORD", s, "password", None),
            keepalive=_resolve("KNOCK_FRIGATE_KEEPALIVE", s, "keepalive", 60, int),
            http_host=_resolve("KNOCK_FRIGATE_HTTP_HOST", s, "http_host", "127.0.0.1"),
            http_port=_resolve("KNOCK_FRIGATE_HTTP_PORT", s, "http_port", 5000, int),
            trigger_labels=(
                trigger_labels_env
                if trigger_labels_env is not None
                else s.get("trigger_labels", ["person"])
            ),
            zones=zones_env if zones_env is not None else s.get("zones", []),
        )


class HomeAssistantConfig(BaseModel):
    """Connection settings for the Home Assistant bridge."""

    base_url: str = "http://homeassistant.local:8123"
    token: SecretStr = SecretStr("")
    trigger_entity_id: str = "binary_sensor.front_doorbell"
    notify_service: str | None = None
    verify_ssl: bool = True

    @classmethod
    def from_env(cls) -> HomeAssistantConfig:
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

    @classmethod
    def from_sources(cls, store: ConfigStore) -> HomeAssistantConfig:
        s = store.get_section("homeassistant")
        return cls(
            base_url=_resolve(
                "KNOCK_HA_BASE_URL", s, "base_url", "http://homeassistant.local:8123"
            ),
            token=_resolve("KNOCK_HA_TOKEN", s, "token", ""),
            trigger_entity_id=_resolve(
                "KNOCK_HA_TRIGGER_ENTITY_ID",
                s,
                "trigger_entity_id",
                "binary_sensor.front_doorbell",
            ),
            notify_service=_resolve("KNOCK_HA_NOTIFY_SERVICE", s, "notify_service", None),
            verify_ssl=_resolve("KNOCK_HA_VERIFY_SSL", s, "verify_ssl", True, _bool),
        )


class UnifiConfig(BaseModel):
    """Connection settings for the UniFi Protect bridge."""

    host: str = "127.0.0.1"
    port: int = 443
    api_key: SecretStr = SecretStr("")
    verify_ssl: bool = False
    trigger_on: list[str] = Field(default_factory=lambda: ["ring"])
    # Which camera(s) a matching trigger_on event actually reacts to.
    # Empty (the default) means every camera on the console -- a smart-
    # detect trigger like "person" otherwise fires from *any* camera that
    # reports it, not just the doorbell, which starts a full conversation
    # every time a driveway or backyard camera sees someone walk by.
    trigger_camera_ids: list[str] = Field(default_factory=list)
    rtsp_quality: str = "high"
    listen_seconds: float = 10.0
    # A physical doorbell chime takes a couple of seconds to finish playing
    # after the `ring` event fires -- without this, KNOCK's greeting starts
    # talking over it, and/or STT's listen window opens early enough to
    # capture the tail of the chime instead of just the visitor. Only
    # applied for an actual `ring` (see UnifiBridge.handle_event); a silent
    # smart-detect trigger has no chime to wait out.
    ring_chime_delay_seconds: float = 2.0
    # Below this RMS loudness, captured audio is treated as silence and
    # never sent to the STT provider -- Whisper (like most STT models) will
    # confidently hallucinate *something* from near-silent audio rather
    # than admit nothing was said, which let the conversation loop in
    # UnifiBridge.handle_event() run its full turn cap instead of
    # recognizing a visitor had gone quiet. Measured against a real
    # doorbell mic: ambient room noise alone reads around RMS 5-6, well
    # under this default, leaving a wide margin before actual speech.
    silence_rms_threshold: float = 60.0
    # Debounces a rapid second `ring` within this many seconds of the
    # session's last turn -- an impatient or accidental double-press
    # otherwise forces a full session restart (see
    # UnifiBridge._start_or_resume_session), re-playing the entire greeting
    # mid-conversation. Deliberately much shorter than
    # `_SESSION_IDLE_TIMEOUT`: this is about debouncing the same visitor
    # mashing the button, not about how long a visit can go quiet before
    # it's considered over -- a `ring` after the cooldown but still within
    # the idle timeout still restarts, since that's plausibly a new visitor.
    ring_cooldown_seconds: float = 15.0

    @classmethod
    def from_env(cls) -> UnifiConfig:
        trigger_on_raw = os.environ.get("KNOCK_UNIFI_TRIGGER_ON")
        trigger_on = (
            [item.strip() for item in trigger_on_raw.split(",") if item.strip()]
            if trigger_on_raw is not None
            else ["ring"]
        )
        trigger_camera_ids_raw = os.environ.get("KNOCK_UNIFI_TRIGGER_CAMERA_IDS")
        trigger_camera_ids = (
            [item.strip() for item in trigger_camera_ids_raw.split(",") if item.strip()]
            if trigger_camera_ids_raw is not None
            else []
        )
        verify_ssl_raw = os.environ.get("KNOCK_UNIFI_VERIFY_SSL", "false").strip().lower()

        return cls(
            host=os.environ.get("KNOCK_UNIFI_HOST", "127.0.0.1"),
            port=int(os.environ.get("KNOCK_UNIFI_PORT", "443")),
            api_key=os.environ.get("KNOCK_UNIFI_API_KEY", ""),
            verify_ssl=verify_ssl_raw not in ("false", "0", "no"),
            trigger_on=trigger_on,
            trigger_camera_ids=trigger_camera_ids,
            rtsp_quality=os.environ.get("KNOCK_UNIFI_RTSP_QUALITY", "high"),
            listen_seconds=float(os.environ.get("KNOCK_UNIFI_LISTEN_SECONDS", "10.0")),
            ring_chime_delay_seconds=float(
                os.environ.get("KNOCK_UNIFI_RING_CHIME_DELAY_SECONDS", "2.0")
            ),
            silence_rms_threshold=float(
                os.environ.get("KNOCK_UNIFI_SILENCE_RMS_THRESHOLD", "60.0")
            ),
            ring_cooldown_seconds=float(
                os.environ.get("KNOCK_UNIFI_RING_COOLDOWN_SECONDS", "15.0")
            ),
        )

    @classmethod
    def from_sources(cls, store: ConfigStore) -> UnifiConfig:
        s = store.get_section("unifi")
        trigger_on_env = _csv(os.environ.get("KNOCK_UNIFI_TRIGGER_ON"))
        trigger_camera_ids_env = _csv(os.environ.get("KNOCK_UNIFI_TRIGGER_CAMERA_IDS"))

        return cls(
            host=_resolve("KNOCK_UNIFI_HOST", s, "host", "127.0.0.1"),
            port=_resolve("KNOCK_UNIFI_PORT", s, "port", 443, int),
            api_key=_resolve("KNOCK_UNIFI_API_KEY", s, "api_key", ""),
            verify_ssl=_resolve("KNOCK_UNIFI_VERIFY_SSL", s, "verify_ssl", False, _bool),
            trigger_on=(
                trigger_on_env if trigger_on_env is not None else s.get("trigger_on", ["ring"])
            ),
            trigger_camera_ids=(
                trigger_camera_ids_env
                if trigger_camera_ids_env is not None
                else s.get("trigger_camera_ids", [])
            ),
            rtsp_quality=_resolve("KNOCK_UNIFI_RTSP_QUALITY", s, "rtsp_quality", "high"),
            listen_seconds=_resolve("KNOCK_UNIFI_LISTEN_SECONDS", s, "listen_seconds", 10.0, float),
            ring_chime_delay_seconds=_resolve(
                "KNOCK_UNIFI_RING_CHIME_DELAY_SECONDS", s, "ring_chime_delay_seconds", 2.0, float
            ),
            silence_rms_threshold=_resolve(
                "KNOCK_UNIFI_SILENCE_RMS_THRESHOLD", s, "silence_rms_threshold", 60.0, float
            ),
            ring_cooldown_seconds=_resolve(
                "KNOCK_UNIFI_RING_COOLDOWN_SECONDS", s, "ring_cooldown_seconds", 15.0, float
            ),
        )
