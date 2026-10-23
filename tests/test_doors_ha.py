"""Phase 3 of plans/doors-routing.md: Home Assistant sensors resolve to
doors. A sensor an explicit door lists as a trigger starts a visit at that
door; every other sensor keeps going through `knock_sensors` as before.
"""

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from knock.config import DoorsConfig, HomeAssistantConfig, SipConfig, UnifiConfig
from knock.conversation.prompts import GREETING
from knock.core.doors import DoorConfig
from knock.core.session_store import JSONFileSessionStore
from knock.integrations.homeassistant import HomeAssistantKnockListener
from knock.integrations.unifi import UnifiBridge
from knock.providers.tts.base import SynthesizedAudio

ENTITY = "binary_sensor.front_knock"
OTHER = "binary_sensor.side_knock"


class _World:
    """A bridge plus a knock listener, with every device call recorded."""

    def __init__(self, tmp_path, doors: list[DoorConfig], *, sensors=None, transcripts=("",)):
        self.calls: list[tuple[str, str]] = []
        self.spoken: list[str] = []
        self.notifier = MagicMock()
        client = MagicMock()

        async def snapshot(device_id: str, package: bool = False) -> bytes:
            self.calls.append(("snapshot", device_id))
            return b"jpeg"

        client.get_public_api_camera_snapshot = AsyncMock(side_effect=snapshot)
        tts = MagicMock()

        async def synthesize(text: str) -> SynthesizedAudio:
            return SynthesizedAudio(audio=b"\x01\x02" * 400, rate=8000, width=2, channels=1)

        tts.synthesize = synthesize
        ha_config = HomeAssistantConfig(knock_sensors=sensors or {})
        self.bridge = UnifiBridge(
            config=UnifiConfig(host="h", port=443, api_key="k", ring_chime_delay_seconds=0.0),
            session_store=JSONFileSessionStore(tmp_path),
            client=client,
            tts_provider=tts,
            stt_provider=MagicMock(),
            vision_provider=MagicMock(),
            ha_notifier=self.notifier,
            doors=DoorsConfig(doors=doors),
            ha_config=ha_config,
        )
        script = iter(transcripts)

        async def listen(device_id: str) -> str:
            self.calls.append(("listen", device_id))
            return next(script, "")

        async def speak(device_id: str, text: str) -> None:
            self.calls.append(("speak", device_id))
            self.spoken.append(text)

        async def thinking(device_id: str) -> None:
            self.calls.append(("thinking", device_id))

        self.bridge.listen_to_visitor = listen  # type: ignore[method-assign]
        self.bridge.speak_to_visitor = speak  # type: ignore[method-assign]
        self.bridge._play_thinking_tone = thinking  # type: ignore[method-assign]
        self.listener = HomeAssistantKnockListener(
            config=ha_config,
            on_knock=self.bridge.handle_knock,
            doors=self.bridge.doors,
            on_door_knock=self.bridge.handle_door_knock,
        )

    def devices(self, role: str) -> set[str]:
        return {device for r, device in self.calls if r == role}

    def knock(self, entity: str = ENTITY, old: str = "off", new: str = "on") -> None:
        message = {
            "type": "event",
            "event": {
                "event_type": "state_changed",
                "data": {
                    "entity_id": entity,
                    "old_state": {"state": old},
                    "new_state": {"state": new},
                },
            },
        }
        asyncio.run(self.listener.handle_message(json.dumps(message)))

    def ring(self, device_id: str = "cam1") -> None:
        event = SimpleNamespace(id="e1", type="ring", device_id=device_id, smart_detect_types=())
        asyncio.run(self.bridge.handle_event(event))


def _door(**overrides: Any) -> DoorConfig:
    fields: dict[str, Any] = {
        "key": "front",
        "name": "Front door",
        "triggers": {"protect_cameras": ["cam1"], "ha_entities": [ENTITY]},
        "listen_device": "mic",
        "speak_device": "speaker",
        "look_devices": ["eyes"],
    }
    return DoorConfig(**{**fields, **overrides})


def test_a_sensor_an_explicit_door_claims_starts_a_visit_on_the_doors_devices(tmp_path) -> None:
    world = _World(tmp_path, [_door()], transcripts=["hello", ""])

    world.knock()

    assert world.devices("listen") == {"mic"}
    assert world.devices("speak") == {"speaker"}
    assert world.devices("snapshot") == {"eyes"}
    assert world.bridge.session_store.load("door-front") is not None


def test_a_sensor_and_a_camera_trigger_share_one_door_session(tmp_path) -> None:
    world = _World(tmp_path, [_door()], transcripts=["hello", "", "hello again", ""])

    world.ring("cam1")
    world.knock()

    state = world.bridge.session_store.load("door-front")
    assert state is not None and state.turn_count >= 2
    assert world.spoken.count(GREETING) == 1  # the knock resumed the visit, no second greeting
    assert world.bridge.session_store.load("unifi-cam1") is None


def test_a_sensor_that_clears_does_not_start_a_second_visit_at_a_door(tmp_path) -> None:
    world = _World(tmp_path, [_door()], transcripts=["hello", ""])

    world.knock(old="off", new="on")
    heard = list(world.calls)
    world.knock(old="on", new="off")

    assert world.calls == heard


def test_a_sensor_no_door_claims_and_knock_sensors_does_not_map_is_ignored(tmp_path) -> None:
    world = _World(tmp_path, [_door()])

    world.knock(OTHER)

    assert world.calls == []


def test_a_knock_sensors_mapping_still_works_alongside_explicit_doors(tmp_path) -> None:
    world = _World(tmp_path, [_door()], sensors={OTHER: "cam9"}, transcripts=["hello", ""])

    world.knock(OTHER)

    for role in ("listen", "speak", "snapshot"):
        assert world.devices(role) == {"cam9"}, role
    assert world.bridge.session_store.load("unifi-cam9") is not None


def test_a_knock_sensor_mapped_to_a_camera_an_explicit_door_claims_joins_that_door(
    tmp_path,
) -> None:
    world = _World(tmp_path, [_door()], sensors={OTHER: "cam1"}, transcripts=["hello", ""])

    world.knock(OTHER)

    assert world.devices("speak") == {"speaker"}
    assert world.bridge.session_store.load("door-front") is not None


def test_a_sensor_only_door_works_when_it_names_every_device(tmp_path) -> None:
    door = DoorConfig(
        key="side",
        triggers={"ha_entities": [OTHER]},
        listen_device="side-mic",
        speak_device="side-speaker",
        look_devices=["side-eyes"],
    )
    world = _World(tmp_path, [door], transcripts=["hello", ""])

    world.knock(OTHER)

    assert world.devices("listen") == {"side-mic"}
    assert world.devices("speak") == {"side-speaker"}
    assert world.devices("snapshot") == {"side-eyes"}
    assert world.bridge.session_store.load("door-side") is not None


def test_a_knock_notification_names_the_door_and_buttons_use_its_speaker(tmp_path) -> None:
    world = _World(tmp_path, [_door()], transcripts=["I have a package that needs a signature", ""])

    world.knock()

    world.notifier.notify.assert_called()
    call = world.notifier.notify.call_args
    assert "(door: Front door)" in call.args[0]
    assert all(a["action"].endswith("speaker") for a in call.kwargs["actions"])


def test_a_knock_at_a_door_rings_the_doors_own_phones(tmp_path) -> None:
    caller = MagicMock()
    caller.configured = True
    caller.config = SipConfig(enabled=True, call_on=["approval"], call_cooldown_seconds=300.0)
    caller.announce.return_value = SimpleNamespace(answered_by="202", attempted=["202"], error=None)
    world = _World(
        tmp_path,
        [_door(call_targets=["202"])],
        transcripts=["I have a package that needs a signature", ""],
    )
    world.bridge.household.sip_caller = caller

    async def scenario() -> None:
        message = {
            "type": "event",
            "event": {
                "event_type": "state_changed",
                "data": {
                    "entity_id": ENTITY,
                    "old_state": {"state": "off"},
                    "new_state": {"state": "on"},
                },
            },
        }
        await world.listener.handle_message(json.dumps(message))
        await asyncio.gather(*world.bridge._call_tasks)

    asyncio.run(scenario())

    caller.announce.assert_called_once()
    assert caller.announce.call_args.kwargs == {"targets": ["202"]}


def test_a_door_knock_is_recorded_against_the_doors_look_camera(tmp_path) -> None:
    from knock.core.audit import JSONLAuditLog

    world = _World(tmp_path, [_door()], transcripts=["hello", ""])
    audit = JSONLAuditLog(tmp_path / "audit.jsonl")
    world.bridge.audit_log = audit

    world.knock()

    [entry] = audit.recent()
    assert entry.session_id == "door-front"
    assert entry.scene is not None and entry.scene.camera == "eyes"


# -- starting the listener ---------------------------------------------------------------------


def _started_knock_listener(tmp_path, doors: list[DoorConfig], sensors: dict[str, str]) -> bool:
    from unittest.mock import patch

    import knock.integrations.unifi as unifi_module

    world = _World(tmp_path, doors, sensors=sensors)
    ha_config = HomeAssistantConfig(token="t", knock_sensors=sensors)
    world.bridge.run = AsyncMock()  # type: ignore[method-assign]
    with (
        patch.object(unifi_module, "HomeAssistantActionListener") as actions,
        patch.object(unifi_module, "HomeAssistantKnockListener") as knocks,
    ):
        actions.return_value.run = AsyncMock()
        knocks.return_value.run = AsyncMock()
        asyncio.run(unifi_module._run_bridge_and_listener(world.bridge, ha_config))
    return knocks.called


def test_the_knock_listener_starts_for_a_sensor_only_door(tmp_path) -> None:
    door = DoorConfig(
        key="side",
        triggers={"ha_entities": [OTHER]},
        listen_device="m",
        speak_device="s",
        look_devices=["e"],
    )
    assert _started_knock_listener(tmp_path, [door], {}) is True


def test_the_knock_listener_stays_off_with_no_sensors_and_no_door_sensors(tmp_path) -> None:
    assert _started_knock_listener(tmp_path / "a", [], {}) is False
    camera_only = _door(triggers={"protect_cameras": ["cam1"]})
    assert _started_knock_listener(tmp_path / "b", [camera_only], {}) is False
