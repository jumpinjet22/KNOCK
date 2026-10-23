"""Pins how a visit is routed today, before the doors refactor
(plans/doors-routing.md, Phase 0).

Every visit runs on a single camera id: whichever camera triggered it (or
the camera a Home Assistant knock sensor maps to) is used for listening,
talking, snapshots, the session, the notification text and package alerts.
With no doors configured, the doors work must keep all of this exactly as
it is -- so these tests only describe current behavior. If one of them has
to change, that's a behavior change, not a refactor.
"""

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from knock.config import HomeAssistantConfig, SipConfig, UnifiConfig
from knock.conversation.prompts import GREETING
from knock.core.events import VisitorEvent
from knock.core.responses import ResponseDecision
from knock.core.session_store import JSONFileSessionStore
from knock.integrations.homeassistant import ACTION_DEVICE_ID_SEP, HomeAssistantKnockListener
from knock.integrations.unifi import UnifiBridge
from knock.providers.tts.base import SynthesizedAudio


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


# -- every role of a visit uses the triggering camera ----------------------------------


def test_a_ring_listens_speaks_and_looks_through_the_ringing_camera(tmp_path) -> None:
    routed = _Routed(tmp_path, transcripts=["I have a package for you", ""])

    decision = asyncio.run(routed.bridge.handle_event(_event(device_id="cam1")))

    assert decision is not None
    assert routed.spoken[0] == GREETING
    for role in ("listen", "speak", "snapshot"):
        assert routed.devices(role) == {"cam1"}, role
    assert routed.bridge.session_store.load("unifi-cam1") is not None


def test_a_smart_detect_trigger_routes_through_the_detecting_camera(tmp_path) -> None:
    routed = _Routed(tmp_path, transcripts=["hello", ""], trigger_on=["ring", "person"])

    decision = asyncio.run(
        routed.bridge.handle_event(_event("smartDetectZone", "cam2", ("person",)))
    )

    assert decision is not None
    for role in ("listen", "speak", "snapshot"):
        assert routed.devices(role) == {"cam2"}, role
    assert routed.bridge.session_store.load("unifi-cam2") is not None


def test_a_ring_on_a_camera_outside_trigger_camera_ids_is_ignored(tmp_path) -> None:
    routed = _Routed(tmp_path, transcripts=["hello", ""], trigger_camera_ids=["cam1"])

    decision = asyncio.run(routed.bridge.handle_event(_event(device_id="cam2")))

    assert decision is None
    assert routed.calls == []
    assert routed.bridge.session_store.load("unifi-cam2") is None


def test_a_knock_sensor_routes_everything_through_its_mapped_camera(tmp_path) -> None:
    routed = _Routed(tmp_path, transcripts=["I have a package for you", ""])
    listener = HomeAssistantKnockListener(
        config=HomeAssistantConfig(knock_sensors={"binary_sensor.knock": "cam3"}),
        on_knock=routed.bridge.handle_knock,
    )
    message = {
        "type": "event",
        "event": {
            "event_type": "state_changed",
            "data": {
                "entity_id": "binary_sensor.knock",
                "old_state": {"state": "off"},
                "new_state": {"state": "on"},
            },
        },
    }

    asyncio.run(listener.handle_message(json.dumps(message)))

    assert routed.spoken[0] == GREETING
    for role in ("listen", "speak", "snapshot"):
        assert routed.devices(role) == {"cam3"}, role
    assert routed.bridge.session_store.load("unifi-cam3") is not None


# -- notifications name the camera -------------------------------------------------------


@pytest.mark.parametrize(
    ("intent", "text", "has_actions"),
    [
        (
            "delivery_signature_required",
            "A delivery at the door needs a signature (camera: cam1).",
            True,
        ),
        ("food_delivery", "A food delivery is at the door (camera: cam1).", True),
        (
            "official_visit",
            "Someone claiming official business is at the door (camera: cam1).",
            False,
        ),
        (
            "suspicious_activity",
            "Possibly concerning activity at the door (camera: cam1).",
            False,
        ),
        ("ride_arrived", "A rideshare/taxi driver is here for pickup (camera: cam1).", False),
        (
            "service_appointment",
            "A technician has arrived for a service appointment (camera: cam1).",
            True,
        ),
    ],
)
def test_turn_notification_text_names_the_camera(tmp_path, intent, text, has_actions) -> None:
    routed = _Routed(tmp_path)
    decision = ResponseDecision(text="ok", intent=intent)
    visitor = VisitorEvent(source="unifi-cam1", text="hi", timestamp=datetime.now(UTC))

    asyncio.run(routed.bridge._handle_turn_notifications(decision, visitor, "cam1"))

    routed.notifier.notify.assert_called_once()
    assert routed.notifier.notify.call_args.args[0] == text
    actions = routed.notifier.notify.call_args.kwargs["actions"] or []
    assert bool(actions) is has_actions
    for action in actions:
        assert action["action"].endswith(f"{ACTION_DEVICE_ID_SEP}cam1")


def test_an_unsure_intent_notification_names_the_camera(tmp_path) -> None:
    routed = _Routed(tmp_path)
    decision = ResponseDecision(
        text="ok", intent="unknown", needs_review=True, review_candidates=["delivery", "visitation"]
    )
    visitor = VisitorEvent(source="unifi-cam1", text="hi", timestamp=datetime.now(UTC))

    asyncio.run(routed.bridge._handle_turn_notifications(decision, visitor, "cam1"))

    assert routed.notifier.notify.call_args.args[0] == (
        "Unsure what a visitor at the door wants (camera: cam1). "
        "(best guesses: delivery, visitation)"
    )


def test_an_emergency_notification_quotes_the_visitor(tmp_path) -> None:
    routed = _Routed(tmp_path)
    decision = ResponseDecision(text="ok", escalate=True, reason="emergency")
    visitor = VisitorEvent(source="unifi-cam1", text="help, fire!", timestamp=datetime.now(UTC))

    asyncio.run(routed.bridge._on_escalate_notify(decision, visitor, "cam1"))

    assert routed.notifier.notify.call_args.args[0] == (
        'Possible emergency at the door: "help, fire!"'
    )
    assert routed.notifier.notify.call_args.kwargs["category"] == "emergency"


# -- package alerts are keyed by camera ----------------------------------------------------


def test_a_package_alert_is_tagged_and_worded_by_camera(tmp_path) -> None:
    routed = _Routed(tmp_path, vision=False)

    asyncio.run(routed.bridge.packages.on_package_detected("cam1"))

    routed.notifier.notify.assert_called_once()
    call = routed.notifier.notify.call_args
    assert call.args[0] == "A package was detected at the door (camera: cam1)."
    assert call.kwargs["tag"] == "knock-package-cam1"
    assert call.kwargs["category"] == "package"
    [ack] = call.kwargs["actions"]
    assert ack["action"].endswith(f"{ACTION_DEVICE_ID_SEP}cam1")


def test_package_presence_is_tracked_per_camera(tmp_path) -> None:
    packages = _Routed(tmp_path, vision=False).bridge.packages

    packages.conversation_started("cam1")

    assert packages.visitor_present("cam1") is True
    assert packages.visitor_present("cam2") is False
    packages.conversation_ended("cam1")
    assert packages.visitor_present("cam1") is False


# -- SIP calls -------------------------------------------------------------------------------


def _sip_caller(call_on: list[str]) -> MagicMock:
    caller = MagicMock()
    caller.configured = True
    caller.config = SipConfig(enabled=True, call_on=call_on, call_cooldown_seconds=300.0)
    caller.announce.return_value = SimpleNamespace(answered_by="101", attempted=["101"], error=None)
    return caller


def test_a_sip_call_speaks_todays_text_for_a_configured_category(tmp_path) -> None:
    caller = _sip_caller(["approval"])
    routed = _Routed(tmp_path, sip_caller=caller)

    async def scenario() -> None:
        await routed.bridge._notify_household("cam1", "Food is here", category="approval")
        await asyncio.gather(*routed.bridge._call_tasks)

    asyncio.run(scenario())

    caller.announce.assert_called_once()
    assert routed.spoken == ["This is KNOCK, about the front door. Food is here"]


def test_no_sip_call_for_a_category_not_configured(tmp_path) -> None:
    caller = _sip_caller(["emergency"])
    routed = _Routed(tmp_path, sip_caller=caller)

    asyncio.run(routed.bridge._notify_household("cam1", "Food is here", category="approval"))

    caller.announce.assert_not_called()
