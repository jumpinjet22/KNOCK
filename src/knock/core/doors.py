"""Doors: a named place a visit happens, with its own triggers and its own
choice of devices to listen, speak and look through.

Today every visit is routed by one camera ID. A `Door` generalizes that: a
door can listen on one camera, talk through another and take pictures from
a third. See plans/doors-routing.md.

The safety contract this module exists to keep: with no doors configured,
behavior is byte-for-byte what it was before doors existed. KNOCK derives
an *implicit* door per camera -- listen = speak = look = that camera, named
after the camera ID, session `unifi-<sanitized camera ID>` -- so anything
built from a door (session IDs, `(camera: <id>)` notification text, package
tags) renders exactly as it did when the camera ID was used directly.

Nothing here is wired into the bridges yet (that's Phase 2): this is the
model, its validation, and the resolution rules, so they can be tested in
isolation first.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from knock.core.session_ids import build_session_id

if TYPE_CHECKING:
    from knock.config import DoorsConfig, HomeAssistantConfig, UnifiConfig

# A door key ends up in a session ID, and session IDs are filenames in
# JSONFileSessionStore -- so a key must never be able to contain a path
# separator or "..".
_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def implicit_session_id(device_id: str) -> str:
    """The session ID a camera has always had -- `unifi-<sanitized id>`."""
    return build_session_id("unifi", device_id)


class DoorTriggers(BaseModel):
    """What can start a visit at a door: Protect cameras (ring/smart-detect)
    and Home Assistant entities (e.g. a knock sensor).
    """

    model_config = ConfigDict(extra="forbid")

    protect_cameras: list[str] = Field(default_factory=list)
    ha_entities: list[str] = Field(default_factory=list)

    @field_validator("protect_cameras", "ha_entities")
    @classmethod
    def _no_blank_or_duplicate(cls, values: list[str]) -> list[str]:
        if any(not v.strip() for v in values):
            raise ValueError("trigger IDs must not be blank")
        if len(set(values)) != len(values):
            raise ValueError("a trigger is listed twice")
        return values

    @property
    def empty(self) -> bool:
        return not self.protect_cameras and not self.ha_entities


class DoorConfig(BaseModel):
    """One user-authored door. `extra="forbid"` so a typo like
    `speek_device` is an error, not a silently ignored field that routes
    audio to the wrong camera.

    Deliberately has no `implicit` flag: that's a property of doors KNOCK
    derives itself (see `Door`). If a config could claim it, two doors
    could end up sharing a session.
    """

    model_config = ConfigDict(extra="forbid")

    key: str
    name: str = ""
    triggers: DoorTriggers = Field(default_factory=DoorTriggers)
    listen_device: str | None = None
    speak_device: str | None = None
    # Ordered; the first is the main snapshot camera.
    look_devices: list[str] = Field(default_factory=list)
    # Per-door SIP targets; empty falls back to `SipConfig.call_targets`.
    call_targets: list[str] = Field(default_factory=list)

    @field_validator("key")
    @classmethod
    def _key_is_safe(cls, key: str) -> str:
        if not _KEY_RE.match(key):
            raise ValueError(
                "door key must be 1-64 characters of letters, digits, '_' or '-' "
                "(it becomes part of a session filename)"
            )
        return key

    @field_validator("look_devices", "call_targets")
    @classmethod
    def _no_blank_or_duplicate(cls, values: list[str]) -> list[str]:
        if any(not v.strip() for v in values):
            raise ValueError("entries must not be blank")
        if len(set(values)) != len(values):
            raise ValueError("an entry is listed twice")
        return values

    @field_validator("listen_device", "speak_device")
    @classmethod
    def _device_not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("device must not be blank")
        return value

    @model_validator(mode="after")
    def _has_triggers_and_devices(self) -> DoorConfig:
        if self.triggers.empty:
            raise ValueError(f"door {self.key!r} has no triggers")
        # Anything left unspecified falls back to one camera: the first look
        # device, else the first Protect camera that triggers this door. A
        # door with neither (e.g. only a Home Assistant sensor) has no
        # camera to fall back to, so it must name every device itself.
        base = self.base_device
        if base is None and not (self.listen_device and self.speak_device and self.look_devices):
            raise ValueError(
                f"door {self.key!r} has no camera trigger to default to, so it needs "
                "listen_device, speak_device and look_devices"
            )
        return self

    @property
    def base_device(self) -> str | None:
        if self.look_devices:
            return self.look_devices[0]
        if self.triggers.protect_cameras:
            return self.triggers.protect_cameras[0]
        return None


@dataclass(frozen=True)
class Door:
    """A resolved door: every device filled in, immutable."""

    key: str
    name: str
    listen_device: str
    speak_device: str
    look_devices: tuple[str, ...]
    call_targets: tuple[str, ...] = ()
    implicit: bool = False

    @property
    def session_id(self) -> str:
        if self.implicit:
            return implicit_session_id(self.listen_device)
        return f"door-{self.key}"


def implicit_door(device_id: str) -> Door:
    """The door a camera has when nothing else claims it. Named after the
    camera ID so any text built from the door name reads exactly like the
    `(camera: <id>)` it replaces.
    """
    return Door(
        key=device_id,
        name=device_id,
        listen_device=device_id,
        speak_device=device_id,
        look_devices=(device_id,),
        implicit=True,
    )


def _explicit_door(config: DoorConfig) -> Door:
    base = config.base_device
    # `base` can only be None if all three were given (validated above).
    listen = config.listen_device or base
    speak = config.speak_device or base
    look = tuple(config.look_devices) or ((base,) if base else ())
    assert listen is not None and speak is not None and look
    return Door(
        key=config.key,
        name=config.name or config.key,
        listen_device=listen,
        speak_device=speak,
        look_devices=look,
        call_targets=tuple(config.call_targets),
        implicit=False,
    )


class DoorRegistry:
    """Looks up the door a trigger belongs to.

    Explicit doors claim their triggers. Everything else keeps working as it
    does today through implicit doors: adding one explicit door never stops
    an unrelated camera or sensor from working.
    """

    def __init__(
        self,
        explicit: list[Door],
        *,
        protect_claims: dict[str, Door],
        ha_claims: dict[str, Door],
        camera_allow_list: list[str],
        knock_sensors: dict[str, str],
    ) -> None:
        self._explicit = explicit
        self._protect_claims = protect_claims
        self._ha_claims = ha_claims
        # `UnifiConfig.trigger_camera_ids`: empty means every camera on the
        # console may trigger, as it always has.
        self._allow_list = set(camera_allow_list)
        self._knock_sensors = knock_sensors
        self._implicit: dict[str, Door] = {}

    def _implicit_for(self, device_id: str) -> Door:
        door = self._implicit.get(device_id)
        if door is None:
            door = self._implicit[device_id] = implicit_door(device_id)
        return door

    def for_protect_event(self, device_id: str) -> Door | None:
        """The door a ring/smart-detect on this Protect camera belongs to.

        An explicit door's claim is authoritative. Otherwise the camera has
        its implicit door -- unless `trigger_camera_ids` is set and doesn't
        list it, which is how a camera is kept from starting visits today.
        """
        claimed = self._protect_claims.get(device_id)
        if claimed is not None:
            return claimed
        if self._allow_list and device_id not in self._allow_list:
            return None
        return self._implicit_for(device_id)

    def for_camera(self, device_id: str) -> Door:
        """The door for a camera that is already known to be triggering,
        with no allow-list check -- an external knock sensor has always
        bypassed `trigger_camera_ids`, so a knock mapped to a camera that
        isn't on the list still gets answered.
        """
        return self._protect_claims.get(device_id) or self._implicit_for(device_id)

    def for_ha_entity(self, entity_id: str) -> Door | None:
        """The door a Home Assistant entity (a knock sensor) belongs to.

        A sensor mapped in `knock_sensors` to camera X resolves to X's
        implicit door -- the very same door a ring on X gets, so a knock and
        a ring on one camera share one visit session, as they do today.
        """
        claimed = self._ha_claims.get(entity_id)
        if claimed is not None:
            return claimed
        camera = self._knock_sensors.get(entity_id)
        if camera is None:
            return None
        return self._implicit_for(camera)

    @property
    def explicit_doors(self) -> list[Door]:
        return list(self._explicit)

    @property
    def has_ha_triggers(self) -> bool:
        """Whether any explicit door is triggered by a Home Assistant entity."""
        return bool(self._ha_claims)


def resolve_doors(
    unifi_config: UnifiConfig,
    ha_config: HomeAssistantConfig,
    doors_config: DoorsConfig,
) -> DoorRegistry:
    explicit: list[Door] = []
    protect_claims: dict[str, Door] = {}
    ha_claims: dict[str, Door] = {}
    for door_config in doors_config.doors:
        door = _explicit_door(door_config)
        explicit.append(door)
        for camera in door_config.triggers.protect_cameras:
            protect_claims[camera] = door
        for entity in door_config.triggers.ha_entities:
            ha_claims[entity] = door
    return DoorRegistry(
        explicit,
        protect_claims=protect_claims,
        ha_claims=ha_claims,
        camera_allow_list=list(unifi_config.trigger_camera_ids),
        knock_sensors=dict(ha_config.knock_sensors),
    )
