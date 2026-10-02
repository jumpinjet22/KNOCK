"""Generic settings CRUD for every `*Config` class in `knock.config`.

One pair of routes (`GET`/`PUT /api/settings/{section}`) drives all eight
sections via reflection over each config class's pydantic fields, rather
than hand-writing eight near-identical route pairs. Gated behind
`require_auth` -- this is exactly the surface (credentials, connection
settings for real hardware) the web UI plan's auth exists to protect.
"""

from __future__ import annotations

import os
import types
import typing
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, SecretStr, ValidationError

from knock.api.auth_routes import CurrentUserDep
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
from knock.providers.tts.kokoro import KokoroTTSProvider

router = APIRouter(prefix="/api/settings", tags=["settings"])

SECTION_CONFIGS: dict[str, type[BaseModel]] = {
    "ollama": OllamaConfig,
    "vision": VisionConfig,
    "whisper": WhisperConfig,
    "kokoro": KokoroConfig,
    "mqtt": MqttConfig,
    "frigate": FrigateConfig,
    "homeassistant": HomeAssistantConfig,
    "unifi": UnifiConfig,
}

# Each config's from_env()/from_sources() env-var prefix -- almost always
# `KNOCK_<SECTION_UPPER>_`, except Home Assistant's shorter `KNOCK_HA_`.
ENV_PREFIXES: dict[str, str] = {
    "ollama": "KNOCK_OLLAMA_",
    "vision": "KNOCK_VISION_",
    "whisper": "KNOCK_WHISPER_",
    "kokoro": "KNOCK_KOKORO_",
    "mqtt": "KNOCK_MQTT_",
    "frigate": "KNOCK_FRIGATE_",
    "homeassistant": "KNOCK_HA_",
    "unifi": "KNOCK_UNIFI_",
}

# Fields that are free-text strings but deserve a multi-line editor in the
# UI rather than a single-line input -- a small, explicit list rather than
# guessing from string length.
_LONG_TEXT_FIELDS = {"prompt"}


def get_config_store() -> ConfigStore:
    return ConfigStore()


ConfigStoreDep = Annotated[ConfigStore, Depends(get_config_store)]


def _unwrap_optional(annotation: Any) -> Any:
    """`X | None` (or `Optional[X]`) -> `X`; anything else unchanged."""
    origin = typing.get_origin(annotation)
    if origin is types.UnionType or origin is typing.Union:
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return args[0]
    return annotation


def _field_type(name: str, annotation: Any) -> str:
    resolved = _unwrap_optional(annotation)
    if resolved is SecretStr:
        return "secret"
    if resolved is bool:
        return "bool"
    if resolved is int:
        return "int"
    if resolved is float:
        return "float"
    if typing.get_origin(resolved) is list:
        return "list_string"
    if name in _LONG_TEXT_FIELDS:
        return "text"
    return "string"


class SettingsField(BaseModel):
    name: str
    type: str
    value: Any = None
    has_value: bool | None = None
    shadowed_by_env: bool = False


class SectionSettings(BaseModel):
    section: str
    fields: list[SettingsField]


class UpdateSettingsRequest(BaseModel):
    values: dict[str, Any]


def _require_section(section: str) -> type[BaseModel]:
    config_cls = SECTION_CONFIGS.get(section)
    if config_cls is None:
        raise HTTPException(status_code=404, detail=f"unknown settings section: {section!r}")
    return config_cls


def _build_section_settings(section: str, store: ConfigStore) -> SectionSettings:
    config_cls = SECTION_CONFIGS[section]
    env_prefix = ENV_PREFIXES[section]
    resolved = config_cls.from_sources(store)  # type: ignore[attr-defined]

    fields: list[SettingsField] = []
    for name, field_info in config_cls.model_fields.items():
        type_tag = _field_type(name, field_info.annotation)
        shadowed = os.environ.get(f"{env_prefix}{name.upper()}") is not None
        raw_value = getattr(resolved, name)

        if type_tag == "secret":
            has_value = bool(raw_value.get_secret_value()) if raw_value is not None else False
            fields.append(
                SettingsField(
                    name=name,
                    type=type_tag,
                    value=None,
                    has_value=has_value,
                    shadowed_by_env=shadowed,
                )
            )
        else:
            fields.append(
                SettingsField(name=name, type=type_tag, value=raw_value, shadowed_by_env=shadowed)
            )
    return SectionSettings(section=section, fields=fields)


def _current_values(resolved: BaseModel, config_cls: type[BaseModel]) -> dict[str, Any]:
    """Like `model_dump()`, but a `SecretStr` field comes back as its real
    string value (via `get_secret_value()`) instead of pydantic's masked
    placeholder -- needed so a partial update can merge against the real
    current value without overwriting a secret with the literal string
    pydantic displays for it.
    """
    values: dict[str, Any] = {}
    for name in config_cls.model_fields:
        value = getattr(resolved, name)
        values[name] = value.get_secret_value() if isinstance(value, SecretStr) else value
    return values


@router.get("/{section}", response_model=SectionSettings)
def get_settings(
    section: str, current_user: CurrentUserDep, *, store: ConfigStoreDep
) -> SectionSettings:
    _require_section(section)
    return _build_section_settings(section, store)


@router.put("/{section}", response_model=SectionSettings)
def update_settings(
    section: str,
    body: UpdateSettingsRequest,
    current_user: CurrentUserDep,
    *,
    store: ConfigStoreDep,
) -> SectionSettings:
    config_cls = _require_section(section)

    unknown_fields = set(body.values) - set(config_cls.model_fields)
    if unknown_fields:
        raise HTTPException(
            status_code=400, detail=f"unknown field(s) for {section!r}: {sorted(unknown_fields)}"
        )

    current = config_cls.from_sources(store)  # type: ignore[attr-defined]
    merged = {**_current_values(current, config_cls), **body.values}

    try:
        validated = config_cls(**merged)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc

    # Persist only the fields this request actually touched (not the full
    # resolved, env-included set), so an env var's value never gets
    # accidentally baked into the store.
    to_store: dict[str, Any] = {}
    for key in body.values:
        value = getattr(validated, key)
        if isinstance(value, SecretStr):
            secret_value = value.get_secret_value()
            if not secret_value:
                continue  # blank -- leave the existing stored secret alone
            to_store[key] = secret_value
        else:
            to_store[key] = value

    store.update_section(section, to_store)
    return _build_section_settings(section, store)


class VoicesResponse(BaseModel):
    voices: list[str]
    error: str | None = None


@router.get("/kokoro/voices", response_model=VoicesResponse)
async def get_kokoro_voices(
    current_user: CurrentUserDep, *, store: ConfigStoreDep
) -> VoicesResponse:
    """Live voice list from the configured Kokoro server, for a real dropdown
    instead of a blind text field. Best-effort: if the server is
    unreachable or misconfigured, this reports the error rather than
    raising -- settings should still be viewable/editable either way.
    """
    config = KokoroConfig.from_sources(store)
    provider = KokoroTTSProvider(config=config)
    try:
        voices = await provider.list_voices()
    except Exception as exc:  # noqa: BLE001 - best-effort, reported not raised
        return VoicesResponse(voices=[], error=str(exc))
    return VoicesResponse(voices=voices)
