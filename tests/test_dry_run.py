"""Phase 4 of plans/doors-routing.md: KNOCK_DRY_RUN resolves the door and
logs what it would do -- and does nothing else. Every "nothing happened"
assertion has a control run proving the same fakes are called when dry run
is off, so a silent fake can't make these pass.
"""

import asyncio
import json
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from uiprotect import EventChange

import knock.integrations.unifi as unifi_module
from knock.config import DoorsConfig, HomeAssistantConfig, KnockConfig, SipConfig, UnifiConfig
from knock.core.audit import JSONLAuditLog
from knock.core.doors import DoorConfig
from knock.core.session_store import JSONFileSessionStore
from knock.integrations.homeassistant import KNOCK_ON_MY_WAY_ACTION, HomeAssistantKnockListener
from knock.integrations.unifi import UnifiBridge
from knock.providers.tts.base import SynthesizedAudio

ENTITY = "binary_sensor.front_knock"


class _World:
    def __init__(self, tmp_path, *, dry_run: bool, doors: list[DoorConfig] | None = None):
        self.tmp_path = tmp_path
        self.calls: list[tuple[str, str]] = []
        self.notifier = MagicMock()
        self.caller = MagicMock()
        self.caller.configured = True
        self.caller.config = SipConfig(enabled=True, call_on=["approval"], call_cooldown_seconds=0)
        self.caller.announce.return_value = SimpleNamespace(
            answered_by="1", attempted=["1"], error=None
        )
        self.synthesized: list[str] = []
        client = MagicMock()

        async def snapshot(device_id: str, package: bool = False) -> bytes:
            self.calls.append(("snapshot", device_id))
            return b"jpeg"

        client.get_public_api_camera_snapshot = AsyncMock(side_effect=snapshot)
        tts = MagicMock()

        async def synthesize(text: str) -> SynthesizedAudio:
            self.synthesized.append(text)
            return SynthesizedAudio(audio=b"\x01\x02" * 400, rate=8000, width=2, channels=1)

        tts.synthesize = synthesize
        ha_config = HomeAssistantConfig(knock_sensors={"binary_sensor.legacy": "cam1"})
        self.audit = JSONLAuditLog(tmp_path / "audit.jsonl")
        self.bridge = UnifiBridge(
            config=UnifiConfig(host="h", port=443, api_key="k", ring_chime_delay_seconds=0.0),
            session_store=JSONFileSessionStore(tmp_path / "sessions"),
            audit_log=self.audit,
            client=client,
            tts_provider=tts,
            stt_provider=MagicMock(),
            vision_provider=MagicMock(),
            ha_notifier=self.notifier,
            sip_caller=self.caller,
            doors=DoorsConfig(doors=doors or []),
            ha_config=ha_config,
            dry_run=dry_run,
        )
        script = iter(["I have a package that needs a signature", ""])

        async def listen(device_id: str) -> str:
            self.calls.append(("listen", device_id))
            return next(script, "")

        async def speak(device_id: str, text: str) -> None:
            self.calls.append(("speak", device_id))

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

    def acted(self) -> bool:
        return bool(
            self.calls
            or self.synthesized
            or self.notifier.notify.called
            or self.caller.announce.called
            or self.audit.recent()
            or (self.tmp_path / "sessions").exists()
            and any((self.tmp_path / "sessions").iterdir())
        )

    def run(self, coro: Any) -> Any:
        async def with_calls() -> Any:
            result = await coro
            await asyncio.gather(*self.bridge._call_tasks)
            return result

        return asyncio.run(with_calls())

    def ring(self, device_id: str = "cam1", types=(), kind: str = "ring") -> Any:
        event = SimpleNamespace(id="e1", type=kind, device_id=device_id, smart_detect_types=types)
        return self.run(self.bridge.handle_event(event))

    def knock(self, entity: str = ENTITY) -> None:
        data = {
            "entity_id": entity,
            "old_state": {"state": "off"},
            "new_state": {"state": "on"},
        }
        message = {"type": "event", "event": {"event_type": "state_changed", "data": data}}
        self.run(self.listener.handle_message(json.dumps(message)))


def _door() -> DoorConfig:
    return DoorConfig(
        key="front",
        triggers={"protect_cameras": ["cam1"], "ha_entities": [ENTITY]},
        listen_device="mic",
        speak_device="speaker",
        look_devices=["eyes"],
    )


# -- control: the same fakes ARE used when dry run is off ------------------------------------


def test_control_without_dry_run_a_ring_acts_on_every_side_effect(tmp_path) -> None:
    world = _World(tmp_path, dry_run=False)

    world.ring()

    assert world.calls  # listened, spoke, looked
    assert world.notifier.notify.called
    assert world.caller.announce.called
    assert world.audit.recent()
    assert world.synthesized


# -- dry run -----------------------------------------------------------------------------------


def test_a_dry_run_ring_does_nothing_and_logs_the_resolved_door(tmp_path, caplog) -> None:
    world = _World(tmp_path, dry_run=True, doors=[_door()])

    with caplog.at_level(logging.INFO, logger="knock.integrations.unifi"):
        decision = world.ring()

    assert decision is None
    assert world.acted() is False
    assert (
        "DRY RUN door=front trigger=ring:cam1 listen=mic speak=speaker look=['eyes'] "
        "session=door-front"
    ) in caplog.text


def test_a_dry_run_ring_on_an_implicit_door_logs_the_camera(tmp_path, caplog) -> None:
    world = _World(tmp_path, dry_run=True)

    with caplog.at_level(logging.INFO, logger="knock.integrations.unifi"):
        world.ring("cam7")

    assert world.acted() is False
    assert (
        "DRY RUN door=cam7 trigger=ring:cam7 listen=cam7 speak=cam7 look=['cam7'] "
        "session=unifi-cam7"
    ) in caplog.text


def test_a_dry_run_sensor_knock_does_nothing_and_logs_the_entity(tmp_path, caplog) -> None:
    world = _World(tmp_path, dry_run=True, doors=[_door()])

    with caplog.at_level(logging.INFO, logger="knock.integrations.unifi"):
        world.knock()

    assert world.acted() is False
    assert (
        f"DRY RUN door=front trigger={ENTITY} listen=mic speak=speaker look=['eyes'] "
        "session=door-front"
    ) in caplog.text


def test_a_dry_run_legacy_knock_sensor_does_nothing(tmp_path, caplog) -> None:
    world = _World(tmp_path, dry_run=True)

    with caplog.at_level(logging.INFO, logger="knock.integrations.unifi"):
        world.knock("binary_sensor.legacy")

    assert world.acted() is False
    assert "DRY RUN door=cam1 trigger=knock:cam1" in caplog.text


def test_a_dry_run_package_detection_sends_no_alert_and_takes_no_snapshot(tmp_path) -> None:
    world = _World(tmp_path, dry_run=True)

    world.ring("cam1", types=("package",), kind="smartDetectZone")

    assert world.acted() is False


def test_control_a_package_detection_alerts_without_dry_run(tmp_path) -> None:
    world = _World(tmp_path, dry_run=False)

    world.ring("cam1", types=("package",), kind="smartDetectZone")

    assert world.notifier.notify.called


def test_dry_run_skips_package_presence_tracking(tmp_path) -> None:
    for dry_run, expected in ((True, 0), (False, 1)):
        world = _World(tmp_path / str(dry_run), dry_run=dry_run)
        tracker = MagicMock()
        world.bridge.packages.track_presence = tracker  # type: ignore[method-assign]
        event = SimpleNamespace(id="e", type="ring", device_id="cam1", smart_detect_types=())

        async def scenario(bridge=world.bridge, event=event) -> None:
            bridge._on_event(event, EventChange.STARTED)
            await asyncio.sleep(0)

        with patch.object(world.bridge, "handle_event", AsyncMock()):
            asyncio.run(scenario())

        assert tracker.call_count == expected


def test_dry_run_ignores_a_notification_button_tap(tmp_path) -> None:
    world = _World(tmp_path, dry_run=True)

    world.run(world.bridge.handle_notification_action(KNOCK_ON_MY_WAY_ACTION, "cam1"))

    assert world.acted() is False


def test_control_a_notification_button_tap_speaks_without_dry_run(tmp_path) -> None:
    world = _World(tmp_path, dry_run=False)

    world.run(world.bridge.handle_notification_action(KNOCK_ON_MY_WAY_ACTION, "cam1"))

    assert ("speak", "cam1") in world.calls


# -- config ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"), [("1", True), ("true", True), ("0", False), ("false", False)]
)
def test_knock_dry_run_env(monkeypatch, raw, expected) -> None:
    monkeypatch.setenv("KNOCK_DRY_RUN", raw)
    assert KnockConfig.from_env().dry_run is expected


def test_dry_run_defaults_off_and_env_beats_the_stored_value(monkeypatch, tmp_path) -> None:
    from knock.core.config_store import ConfigStore

    monkeypatch.delenv("KNOCK_DRY_RUN", raising=False)
    assert KnockConfig.from_env().dry_run is False
    store = ConfigStore(tmp_path / "c.json")
    store.set_section("knock", {"dry_run": True})
    assert KnockConfig.from_sources(store).dry_run is True
    monkeypatch.setenv("KNOCK_DRY_RUN", "0")
    assert KnockConfig.from_sources(store).dry_run is False


def test_main_passes_dry_run_to_the_bridge(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("KNOCK_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    for value, expected in (("1", True), ("0", False)):
        monkeypatch.setenv("KNOCK_DRY_RUN", value)
        with (
            patch.object(unifi_module, "UnifiBridge") as bridge_cls,
            patch.object(unifi_module.asyncio, "run", side_effect=lambda coro: coro.close()),
        ):
            unifi_module.main()
        assert bridge_cls.call_args.kwargs["dry_run"] is expected
