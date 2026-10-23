"""Phase 2 of plans/doors-routing.md: a visit runs through a resolved Door.

Phase 0's characterization tests (test_routing_characterization.py) prove
that with no doors configured nothing changed. These cover what only
explicit doors can do: split a visit across devices, share a session, say
the door's name, and use their own phones.
"""

import asyncio
from datetime import UTC, datetime
from typing import Any, cast

from knock.config import DoorsConfig, HomeAssistantConfig
from knock.core.doors import DoorConfig, resolve_doors
from knock.core.events import VisitorEvent
from knock.core.responses import ResponseDecision
from tests.test_routing_characterization import _event, _Routed, _sip_caller


def _split_door(**overrides) -> DoorConfig:
    fields = {
        "key": "front",
        "name": "Front door",
        "triggers": {"protect_cameras": ["cam1"]},
        "listen_device": "mic",
        "speak_device": "speaker",
        "look_devices": ["eyes", "extra"],
    }
    return DoorConfig(**{**fields, **overrides})


def _routed(tmp_path, *doors: DoorConfig, **kw) -> _Routed:
    routed = _Routed(tmp_path, **kw)
    routed.bridge.doors = resolve_doors(
        routed.bridge.config, HomeAssistantConfig(), DoorsConfig(doors=list(doors))
    )
    return routed


def test_an_explicit_door_listens_speaks_and_looks_through_its_own_devices(tmp_path) -> None:
    routed = _routed(tmp_path, _split_door(), transcripts=["I have a package for you", ""])

    asyncio.run(routed.bridge.handle_event(_event(device_id="cam1")))

    assert routed.devices("listen") == {"mic"}
    assert routed.devices("speak") == {"speaker"}
    assert routed.devices("thinking") <= {"speaker"}
    assert routed.devices("snapshot") == {"eyes"}


def test_an_explicit_door_keeps_its_visit_in_one_door_session(tmp_path) -> None:
    routed = _routed(tmp_path, _split_door(), transcripts=["hello", ""])

    asyncio.run(routed.bridge.handle_event(_event(device_id="cam1")))

    assert routed.bridge.session_store.load("door-front") is not None
    assert routed.bridge.session_store.load("unifi-cam1") is None


def test_a_knock_on_a_claimed_camera_joins_the_doors_session(tmp_path) -> None:
    routed = _routed(tmp_path, _split_door(), transcripts=["hello", ""])

    asyncio.run(routed.bridge.handle_knock("cam1"))

    assert routed.bridge.session_store.load("door-front") is not None
    assert routed.devices("speak") == {"speaker"}


def test_other_cameras_are_unaffected_by_an_explicit_door(tmp_path) -> None:
    routed = _routed(tmp_path, _split_door(), transcripts=["hello", ""])

    asyncio.run(routed.bridge.handle_event(_event(device_id="cam2")))

    for role in ("listen", "speak", "snapshot"):
        assert routed.devices(role) == {"cam2"}, role
    assert routed.bridge.session_store.load("unifi-cam2") is not None


def test_a_ha_sensor_and_a_camera_trigger_resolve_to_the_same_door(tmp_path) -> None:
    door = _split_door(triggers={"protect_cameras": ["cam1"], "ha_entities": ["binary_sensor.k"]})
    routed = _routed(tmp_path, door)

    from_sensor = routed.bridge.doors.for_ha_entity("binary_sensor.k")

    assert from_sensor is routed.bridge.doors.for_protect_event("cam1")
    assert from_sensor is not None and from_sensor.session_id == "door-front"


def test_explicit_door_notifications_say_the_door_name_and_use_the_look_camera(tmp_path) -> None:
    routed = _routed(tmp_path, _split_door())
    door = routed.bridge.doors.for_protect_event("cam1")
    decision = ResponseDecision(text="ok", intent="food_delivery")
    visitor = VisitorEvent(source="x", text="hi", timestamp=datetime.now(UTC))

    asyncio.run(routed.bridge._handle_turn_notifications(decision, visitor, "eyes", door))

    text = routed.notifier.notify.call_args.args[0]
    assert text == "A food delivery is at the door (door: Front door)."
    # The buttons talk back through the door's speaker.
    actions = routed.notifier.notify.call_args.kwargs["actions"]
    assert all(a["action"].endswith("speaker") for a in actions)


def test_package_presence_for_an_explicit_door_is_keyed_by_its_look_camera(tmp_path) -> None:
    routed = _routed(tmp_path, _split_door(), transcripts=["hello", ""])
    seen: list[tuple[str, str]] = []
    packages = cast(Any, routed.bridge.packages)
    started, ended = packages.conversation_started, packages.conversation_ended

    def record_start(device_id: str) -> None:
        seen.append(("start", device_id))
        started(device_id)

    def record_end(device_id: str) -> None:
        seen.append(("end", device_id))
        ended(device_id)

    packages.conversation_started = record_start
    packages.conversation_ended = record_end

    asyncio.run(routed.bridge.handle_event(_event(device_id="cam1")))

    assert seen == [("start", "eyes"), ("end", "eyes")]


def test_a_doors_own_call_targets_replace_the_global_list(tmp_path) -> None:
    caller = _sip_caller(["approval"])
    routed = _routed(tmp_path, _split_door(call_targets=["202"]), sip_caller=caller)
    door = routed.bridge.doors.for_protect_event("cam1")

    async def scenario() -> None:
        await routed.bridge._notify_household("eyes", "Food", category="approval", door=door)
        await asyncio.gather(*routed.bridge._call_tasks)

    asyncio.run(scenario())

    assert caller.announce.call_args.kwargs == {"targets": ["202"]}


def test_a_door_without_targets_announces_exactly_as_before(tmp_path) -> None:
    caller = _sip_caller(["approval"])
    routed = _routed(tmp_path, _split_door(), sip_caller=caller)
    door = routed.bridge.doors.for_protect_event("cam1")

    async def scenario() -> None:
        await routed.bridge._notify_household("eyes", "Food", category="approval", door=door)
        await asyncio.gather(*routed.bridge._call_tasks)

    asyncio.run(scenario())

    assert caller.announce.call_args.kwargs == {}
    assert len(caller.announce.call_args.args) == 1


def test_a_camera_claimed_by_a_door_triggers_even_off_the_allow_list(tmp_path) -> None:
    routed = _Routed(tmp_path, trigger_camera_ids=["other"])
    assert routed.bridge.should_trigger(_event(device_id="cam1")) is False
    claimed = _routed(tmp_path, _split_door(), trigger_camera_ids=["other"])
    assert claimed.bridge.should_trigger(_event(device_id="cam1")) is True


def test_announce_rings_only_the_given_targets_when_a_door_overrides_them(tmp_path) -> None:
    from tests.test_sip_caller import _answers, _caller

    uri = "sip:101@192.0.2.10"
    caller, control, _ = _caller(tmp_path, {uri: _answers(uri)})

    outcome = caller.announce(b"\x10\x00" * 800, targets=["101"])

    assert outcome.attempted == ["101"]
    assert control.commands == [("dial", uri)]


def test_a_knock_still_bypasses_the_camera_allow_list(tmp_path) -> None:
    # A knock sensor is configured for one specific camera, so it has always
    # been answered even when that camera isn't in trigger_camera_ids.
    routed = _Routed(tmp_path, transcripts=["hello", ""], trigger_camera_ids=["other"])

    decision = asyncio.run(routed.bridge.handle_knock("cam3"))

    assert decision is not None
    assert routed.devices("speak") == {"cam3"}
