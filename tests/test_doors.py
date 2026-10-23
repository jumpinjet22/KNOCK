"""Phase 1 of plans/doors-routing.md: the Door model and implicit-door
derivation. Nothing here touches the bridges -- the point is that with no
doors configured, every camera resolves to a door that renders exactly as
the bare camera ID does today.
"""

import dataclasses

import pytest
from pydantic import ValidationError

from knock.api.settings_routes import ENV_PREFIXES, SECTION_CONFIGS
from knock.config import DoorsConfig, HomeAssistantConfig, UnifiConfig
from knock.core.config_store import ConfigStore
from knock.core.doors import DoorConfig, resolve_doors
from knock.integrations.unifi import _default_session_id


def _registry(doors=(), *, cameras=(), sensors=None):
    return resolve_doors(
        UnifiConfig(host="h", api_key="k", trigger_camera_ids=list(cameras)),
        HomeAssistantConfig(knock_sensors=sensors or {}),
        DoorsConfig(doors=list(doors)),
    )


def _door(key="front", **kw):
    kw.setdefault("triggers", {"protect_cameras": ["cam1"]})
    return DoorConfig(key=key, **kw)


# -- implicit doors reproduce today's behavior ---------------------------------------------


@pytest.mark.parametrize(
    "device_id", ["cam1", "abc.def/ghi", "", "../x", "é cam", "a" * 300, "UPPER_lower-9"]
)
def test_implicit_session_id_matches_the_bridge(device_id) -> None:
    door = _registry().for_protect_event(device_id)
    assert door is not None
    assert door.session_id == _default_session_id(device_id)


def test_an_implicit_door_uses_one_camera_for_every_role() -> None:
    door = _registry().for_protect_event("cam1")
    assert door is not None
    assert (door.listen_device, door.speak_device, door.look_devices) == ("cam1", "cam1", ("cam1",))
    assert door.name == "cam1"
    assert door.implicit is True


def test_a_knock_sensor_resolves_to_the_same_door_as_a_ring_on_its_camera() -> None:
    registry = _registry(sensors={"binary_sensor.knock": "cam3"})
    assert registry.for_ha_entity("binary_sensor.knock") is registry.for_protect_event("cam3")


def test_unknown_entity_and_excluded_camera_resolve_to_nothing() -> None:
    registry = _registry(cameras=["cam1"], sensors={"binary_sensor.knock": "cam3"})
    assert registry.for_ha_entity("binary_sensor.other") is None
    assert registry.for_protect_event("cam2") is None
    assert registry.for_protect_event("cam1") is not None


# -- explicit doors -------------------------------------------------------------------------


def test_an_explicit_door_defaults_every_role_to_its_camera() -> None:
    door = _registry([_door()]).for_protect_event("cam1")
    assert door is not None
    assert (door.listen_device, door.speak_device, door.look_devices) == ("cam1", "cam1", ("cam1",))
    assert door.session_id == "door-front"
    assert door.implicit is False


def test_an_explicit_door_can_split_roles_across_devices() -> None:
    config = _door(
        listen_device="doorbell",
        speak_device="porch",
        look_devices=["porch", "doorbell"],
        call_targets=["101"],
        name="Front door",
    )
    door = _registry([config]).for_protect_event("cam1")
    assert door is not None
    assert door.listen_device == "doorbell"
    assert door.speak_device == "porch"
    assert door.look_devices == ("porch", "doorbell")
    assert door.call_targets == ("101",)
    assert door.name == "Front door"


def test_an_ha_only_door_works_when_it_names_every_device() -> None:
    config = DoorConfig(
        key="side",
        triggers={"ha_entities": ["binary_sensor.side"]},
        listen_device="c",
        speak_device="c",
        look_devices=["c"],
    )
    door = _registry([config]).for_ha_entity("binary_sensor.side")
    assert door is not None and door.session_id == "door-side"


def test_adding_an_explicit_door_does_not_change_other_cameras() -> None:
    before = _registry(sensors={"binary_sensor.knock": "cam3"})
    after = _registry([_door()], sensors={"binary_sensor.knock": "cam3"})
    for camera in ("cam2", "cam3"):
        assert after.for_protect_event(camera) == before.for_protect_event(camera)
    assert after.for_ha_entity("binary_sensor.knock") == before.for_ha_entity("binary_sensor.knock")


def test_an_explicit_claim_beats_the_camera_allow_list() -> None:
    registry = _registry([_door()], cameras=["cam9"])
    assert registry.for_protect_event("cam1") is not None
    assert registry.for_protect_event("cam2") is None


def test_an_explicit_door_claims_a_sensor_over_knock_sensors() -> None:
    config = DoorConfig(
        key="side",
        triggers={"ha_entities": ["binary_sensor.knock"]},
        look_devices=["cam4"],
        listen_device="cam4",
        speak_device="cam4",
    )
    door = _registry([config], sensors={"binary_sensor.knock": "cam3"}).for_ha_entity(
        "binary_sensor.knock"
    )
    assert door is not None and door.key == "side"


def test_a_resolved_door_is_immutable() -> None:
    door = _registry().for_protect_event("cam1")
    assert door is not None
    with pytest.raises(dataclasses.FrozenInstanceError):
        door.speak_device = "other"


# -- validation -----------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["../x", "a/b", "", "a b", "a.b", "x" * 65, "é"])
def test_unsafe_door_keys_are_rejected(key) -> None:
    with pytest.raises(ValidationError):
        _door(key=key)


def test_a_typo_in_a_field_is_an_error() -> None:
    with pytest.raises(ValidationError):
        _door(speek_device="cam1")
    with pytest.raises(ValidationError):
        DoorConfig(key="x", triggers={"protect_camera": ["cam1"]})


def test_a_door_with_no_triggers_is_rejected() -> None:
    with pytest.raises(ValidationError, match="no triggers"):
        DoorConfig(key="x", triggers={})


def test_an_ha_only_door_missing_devices_is_rejected() -> None:
    with pytest.raises(ValidationError, match="listen_device"):
        DoorConfig(key="x", triggers={"ha_entities": ["binary_sensor.a"]})
    with pytest.raises(ValidationError, match="listen_device"):
        DoorConfig(
            key="x",
            triggers={"ha_entities": ["binary_sensor.a"]},
            listen_device="c",
            speak_device="c",
        )


def test_blank_and_duplicate_entries_are_rejected() -> None:
    with pytest.raises(ValidationError):
        _door(triggers={"protect_cameras": ["cam1", "cam1"]})
    with pytest.raises(ValidationError):
        _door(triggers={"protect_cameras": [" "]})
    with pytest.raises(ValidationError):
        _door(look_devices=["a", "a"])
    with pytest.raises(ValidationError):
        _door(speak_device=" ")


def test_duplicate_keys_and_shared_triggers_are_rejected() -> None:
    with pytest.raises(ValidationError, match="used twice"):
        DoorsConfig(doors=[_door("a"), _door("a", triggers={"protect_cameras": ["cam2"]})])
    with pytest.raises(ValidationError, match="triggers both"):
        DoorsConfig(doors=[_door("a"), _door("b")])
    ha = {"ha_entities": ["binary_sensor.k"]}
    devices = {"listen_device": "c", "speak_device": "c", "look_devices": ["c"]}
    with pytest.raises(ValidationError, match="triggers both"):
        DoorsConfig(
            doors=[
                DoorConfig(key="a", triggers=ha, **devices),
                DoorConfig(key="b", triggers=ha, **devices),
            ]
        )


def test_user_config_cannot_claim_to_be_implicit() -> None:
    with pytest.raises(ValidationError):
        _door(implicit=True)


# -- loading --------------------------------------------------------------------------------

_JSON = '[{"key": "front", "triggers": {"protect_cameras": ["cam1"]}}]'


def test_no_setting_means_no_doors(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("KNOCK_DOORS", raising=False)
    assert DoorsConfig.from_env().doors == []
    assert DoorsConfig.from_sources(ConfigStore(tmp_path / "c.json")).doors == []


def test_env_json_is_loaded_and_blank_means_none(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("KNOCK_DOORS", _JSON)
    assert [d.key for d in DoorsConfig.from_env().doors] == ["front"]
    monkeypatch.setenv("KNOCK_DOORS", "  ")
    assert DoorsConfig.from_env().doors == []
    assert DoorsConfig.from_sources(ConfigStore(tmp_path / "c.json")).doors == []


def test_env_beats_the_stored_section(monkeypatch, tmp_path) -> None:
    store = ConfigStore(tmp_path / "c.json")
    store.set_section(
        "doors", {"doors": [{"key": "stored", "triggers": {"protect_cameras": ["c"]}}]}
    )
    monkeypatch.delenv("KNOCK_DOORS", raising=False)
    assert [d.key for d in DoorsConfig.from_sources(store).doors] == ["stored"]
    monkeypatch.setenv("KNOCK_DOORS", _JSON)
    assert [d.key for d in DoorsConfig.from_sources(store).doors] == ["front"]


@pytest.mark.parametrize("raw", ["{not json", '{"key": "x"}', '"front"'])
def test_malformed_doors_fail_loudly(monkeypatch, raw) -> None:
    monkeypatch.setenv("KNOCK_DOORS", raw)
    with pytest.raises(ValueError):
        DoorsConfig.from_env()


def test_doors_are_not_in_the_settings_ui_yet() -> None:
    assert "doors" not in SECTION_CONFIGS
    assert "doors" not in ENV_PREFIXES
