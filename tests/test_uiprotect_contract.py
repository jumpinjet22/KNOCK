"""Contract tests against the real `uiprotect` types the UniFi bridge depends on.

The rest of the UniFi tests drive the bridge with plain stand-ins
(`SimpleNamespace` events, `MagicMock` clients), so a uiprotect upgrade that
renamed an event field, changed how its enums stringify, or reshaped a client
method would still pass them. These build real uiprotect objects and check
the library's API surface instead, so a uiprotect bump that breaks the bridge
fails CI rather than failing at the front door.

Still no substitute for a real ring: the live events websocket and talkback
audio need actual hardware.
"""

import asyncio
import inspect
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from uiprotect import EventChange, ProtectApiClient, ProtectEvent, ProtectEventChannel
from uiprotect.data.public_devices import PublicCamera, PublicCameraFeatureFlags, RTSPSStreams
from uiprotect.data.types import EventType, SmartDetectObjectType
from uiprotect.stream import TalkbackStream

from knock.config import UnifiConfig
from knock.core.session_store import JSONFileSessionStore
from knock.integrations.unifi import UnifiBridge


def _protect_event(
    event_type: EventType,
    smart_detect_types: tuple[SmartDetectObjectType, ...] = (),
    channel: ProtectEventChannel = ProtectEventChannel.DETECTION,
) -> ProtectEvent:
    return ProtectEvent(
        id="evt1",
        type=event_type,
        channel=channel,
        device_id="cam1",
        start=datetime.now(UTC),
        smart_detect_types=smart_detect_types,
    )


def _bridge(tmp_path, **config_overrides) -> UnifiBridge:
    config = UnifiConfig(
        host="127.0.0.1",
        port=443,
        api_key="test-key",
        ring_chime_delay_seconds=0.0,
        **config_overrides,
    )
    return UnifiBridge(
        config=config, session_store=JSONFileSessionStore(tmp_path), client=MagicMock()
    )


# -- real ProtectEvent through the bridge ----------------------------------------


def test_real_ring_event_triggers_and_renders(tmp_path) -> None:
    bridge = _bridge(tmp_path)
    event = _protect_event(EventType.RING)

    assert bridge.should_trigger(event) is True
    assert bridge.build_event(event).text == "Doorbell ring on cam1"


def test_real_smart_detect_event_triggers_and_renders(tmp_path) -> None:
    bridge = _bridge(tmp_path, trigger_on=["ring", "person"])
    event = _protect_event(EventType.SMART_DETECT, (SmartDetectObjectType.PERSON,))

    assert bridge.should_trigger(event) is True
    assert bridge.build_event(event).text == "person detected on cam1"


def test_real_motion_event_does_not_trigger(tmp_path) -> None:
    assert _bridge(tmp_path).should_trigger(_protect_event(EventType.MOTION)) is False


def test_real_ring_event_through_on_event_starts_a_session(tmp_path) -> None:
    bridge = _bridge(tmp_path)

    async def scenario() -> None:
        bridge._on_event(_protect_event(EventType.RING), EventChange.STARTED)
        current = asyncio.current_task()
        await asyncio.gather(*(t for t in asyncio.all_tasks() if t is not current))

    asyncio.run(scenario())

    assert bridge.session_store.load("unifi-cam1") is not None


# -- client / talkback / camera API surface --------------------------------------


def test_bridge_builds_a_real_client_from_config(tmp_path) -> None:
    bridge = UnifiBridge(
        config=UnifiConfig(host="127.0.0.1", port=443, api_key="test-key"),
        session_store=JSONFileSessionStore(tmp_path),
    )

    assert isinstance(bridge.client, ProtectApiClient)


@pytest.mark.parametrize(
    ("method", "args", "kwargs"),
    [
        ("subscribe_events_and_prime", (lambda event, change: None,), {}),
        ("get_public_api_camera_snapshot", ("cam1",), {"package": True}),
        ("create_talkback_session_public", ("cam1",), {}),
        ("get_cameras_public", (), {}),
        ("close_session", (), {}),
    ],
)
def test_client_methods_the_bridge_calls_still_fit(method, args, kwargs) -> None:
    fn = getattr(ProtectApiClient, method)

    assert inspect.iscoroutinefunction(fn)
    inspect.signature(fn).bind(None, *args, **kwargs)


def test_client_exposes_public_bootstrap() -> None:
    assert isinstance(inspect.getattr_static(ProtectApiClient, "public_bootstrap"), property)


def test_talkback_stream_accepts_camera_path_and_session() -> None:
    inspect.signature(TalkbackStream).bind(MagicMock(), "/tmp/response.wav", None)


def test_public_camera_still_has_the_fields_the_bridge_reads() -> None:
    assert {"name", "feature_flags", "rtsps_streams"} <= set(PublicCamera.model_fields)
    assert "has_speaker" in PublicCameraFeatureFlags.model_fields
    assert isinstance(inspect.getattr_static(PublicCamera, "is_reachable"), property)
    inspect.signature(RTSPSStreams.get_stream_url).bind(None, "high")


@pytest.mark.parametrize("change", ["STARTED", "UPDATED", "ENDED", "REMOVED"])
def test_event_change_members_still_exist(change) -> None:
    assert hasattr(EventChange, change)
