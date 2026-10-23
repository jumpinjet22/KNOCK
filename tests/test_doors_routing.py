"""Phase 2 of plans/doors-routing.md: a visit runs through a resolved Door.

Phase 0's characterization tests (test_routing_characterization.py) prove
that with no doors configured nothing changed. These cover what only
explicit doors can do: split a visit across devices, share a session, say
the door's name, and use their own phones.
"""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

from knock.config import DoorsConfig, HomeAssistantConfig, SipConfig, UnifiConfig
from knock.core.doors import DoorConfig, resolve_doors
from knock.core.events import VisitorEvent
from knock.core.responses import ResponseDecision
from knock.core.session_store import JSONFileSessionStore
from knock.integrations.unifi import UnifiBridge
from knock.providers.tts.base import SynthesizedAudio

# -- fixtures (same shape as test_routing_characterization.py's, kept local so this
# file doesn't depend on another test module's import path) ----------------------


def _event(event_type: str = "ring", device_id: str = "cam1", smart=()) -> SimpleNamespace:
    return SimpleNamespace(
        id="evt1", type=event_type, device_id=device_id, smart_detect_types=smart
    )


class _Routed:
    """A bridge whose camera-facing calls record which device they used."""

    def __init__(
        self, tmp_path, *, transcripts=("",), vision=True, sip_caller=None, **config
    ) -> None:
        self.calls: list[tuple] = []
        self.spoken: list[str] = []
        self.notifier = MagicMock()
        client = MagicMock()

        async def snapshot(device_id: str, package: bool = False) -> bytes:
            self.calls.append(("snapshot", device_id, package))
            return b"fake-jpeg-bytes"

        client.get_public_api_camera_snapshot = AsyncMock(side_effect=snapshot)

        tts = MagicMock()

        async def synthesize(text: str) -> SynthesizedAudio:
            self.spoken.append(text)
            return SynthesizedAudio(audio=b"\x01\x02" * 400, rate=8000, width=2, channels=1)

        tts.synthesize = synthesize

        self.bridge = UnifiBridge(
            config=UnifiConfig(
                host="127.0.0.1", port=443, api_key="k", ring_chime_delay_seconds=0.0, **config
            ),
            session_store=JSONFileSessionStore(tmp_path),
            client=client,
            tts_provider=tts,
            stt_provider=MagicMock(),
            vision_provider=MagicMock() if vision else None,
            ha_notifier=self.notifier,
            sip_caller=sip_caller,
        )
        remaining = iter(transcripts)

        async def listen(device_id: str) -> str:
            self.calls.append(("listen", device_id))
            return next(remaining, "")

        async def speak(device_id: str, text: str) -> None:
            self.calls.append(("speak", device_id))
            self.spoken.append(text)

        async def thinking(device_id: str) -> None:
            self.calls.append(("thinking", device_id))

        self.bridge.listen_to_visitor = listen  # type: ignore[method-assign]
        self.bridge.speak_to_visitor = speak  # type: ignore[method-assign]
        self.bridge._play_thinking_tone = thinking  # type: ignore[method-assign]

    def devices(self, role: str) -> set[str]:
        return {call[1] for call in self.calls if call[0] == role}


def _sip_caller(call_on: list[str]) -> MagicMock:
    caller = MagicMock()
    caller.configured = True
    caller.config = SipConfig(enabled=True, call_on=call_on, call_cooldown_seconds=300.0)
    caller.announce.return_value = SimpleNamespace(answered_by="101", attempted=["101"], error=None)
    return caller


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


def test_a_knock_still_bypasses_the_camera_allow_list(tmp_path) -> None:
    # A knock sensor is configured for one specific camera, so it has always
    # been answered even when that camera isn't in trigger_camera_ids.
    routed = _Routed(tmp_path, transcripts=["hello", ""], trigger_camera_ids=["other"])

    decision = asyncio.run(routed.bridge.handle_knock("cam3"))

    assert decision is not None
    assert routed.devices("speak") == {"cam3"}
