"""The household notifier on its own -- no UniFi bridge involved. The
behavior it must keep (wording, tags, SIP text) is also pinned through the
bridge by test_routing_characterization.py and test_sip_caller.py.
"""

import asyncio
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from knock.core.events import VisitorEvent
from knock.core.responses import ResponseDecision
from knock.integrations.household import HouseholdNotifier


def _household(snapshots: list[tuple[str, bool]], **kw) -> tuple[HouseholdNotifier, MagicMock]:
    notifier = MagicMock()

    async def fetch(device_id: str, package: bool) -> bytes:
        snapshots.append((device_id, package))
        return b"jpeg"

    details = MagicMock()
    details.side_effect = lambda text, intent, *, fallback, scene: MagicMock(summary=fallback)
    household = HouseholdNotifier(
        fetch_snapshot=fetch,
        extract_details=details,
        ha_notifier=notifier,
        public_base_url="https://knock.example.com",
        **kw,
    )
    return household, notifier


def _visitor() -> VisitorEvent:
    return VisitorEvent(source="x", text="hi", timestamp=datetime.now(UTC))


@pytest.mark.parametrize(
    ("intent", "package_lens"),
    [
        ("food_delivery", True),
        ("delivery_signature_required", True),
        ("official_visit", False),
        ("suspicious_activity", False),
        ("service_appointment", False),
    ],
)
def test_deliveries_attach_the_package_camera_photo_and_others_the_main_one(
    intent, package_lens
) -> None:
    snapshots: list[tuple[str, bool]] = []
    household, notifier = _household(snapshots)

    asyncio.run(
        household._handle_turn_notifications(
            ResponseDecision(text="ok", intent=intent), _visitor(), "cam1"
        )
    )

    assert snapshots == [("cam1", package_lens)]
    assert notifier.notify.call_args.kwargs["image"].startswith(
        "https://knock.example.com/api/snapshot/"
    )


def test_no_public_url_means_no_photo_and_no_snapshot_fetch() -> None:
    snapshots: list[tuple[str, bool]] = []
    household, notifier = _household(snapshots)
    household.public_base_url = None

    asyncio.run(household._notify_household("cam1", "hello"))

    assert snapshots == []
    notifier.notify.assert_called_once()


def test_a_failing_snapshot_still_sends_the_notification() -> None:
    household, notifier = _household([])

    async def broken(device_id: str, package: bool) -> bytes:
        raise RuntimeError("camera offline")

    household._fetch_snapshot = broken
    decision = ResponseDecision(text="ok", intent="official_visit")

    asyncio.run(household._handle_turn_notifications(decision, _visitor(), "cam1"))

    notifier.notify.assert_called_once()
    assert notifier.notify.call_args.kwargs["image"] is None


def test_set_down_action_speaks_the_set_down_phrase(tmp_path) -> None:
    import asyncio
    from unittest.mock import MagicMock

    from knock.config import UnifiConfig
    from knock.core.session_store import JSONFileSessionStore
    from knock.integrations.unifi import UnifiBridge

    bridge = UnifiBridge(
        config=UnifiConfig(host="127.0.0.1", port=443, api_key="k", ring_chime_delay_seconds=0.0),
        session_store=JSONFileSessionStore(tmp_path),
        client=MagicMock(),
    )
    spoken: list[tuple[str, str]] = []

    async def fake_speak(device_id: str, text: str) -> None:
        spoken.append((device_id, text))

    bridge.speak_to_visitor = fake_speak  # type: ignore[method-assign]

    asyncio.run(bridge.handle_notification_action("knock_set_down", "cam1"))

    assert spoken == [("cam1", "The homeowner says to find a safe spot to set it down. Thank you!")]
