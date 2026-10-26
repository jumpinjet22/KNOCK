"""Reminders, "gone without a Got it" detection, evidence and clips --
see `knock.integrations.package_alerts`. The detect-and-notify basics are
covered in test_unifi_bridge.py.
"""

import asyncio
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import av
import pytest
from uiprotect import EventChange

import knock.integrations.package_alerts as package_alerts_module
import knock.integrations.unifi as unifi_module
from knock.config import UnifiConfig
from knock.core.package_log import JSONLPackageLog
from knock.core.scene import PackageLabel, SceneObservation
from knock.core.session_store import JSONFileSessionStore
from knock.core.snapshot_store import SnapshotStore
from knock.integrations.package_alerts import record_rtsp_clip
from knock.integrations.unifi import UnifiBridge

_GONE = SceneObservation(package_visible=False, summary="An empty porch.")
_THERE = SceneObservation(package_visible=True, summary="A box by the door.")


class _Vision:
    """Answers each package-camera look with the next observation in line
    (the last one repeats).
    """

    name = "fake-vision"

    def __init__(self, *observations: SceneObservation) -> None:
        self._observations = list(observations)
        self.looks = 0

    def describe(self, image: bytes, prompt: str | None = None) -> str:
        raise AssertionError("observe() should be used")

    def observe(self, image: bytes) -> SceneObservation:
        self.looks += 1
        return self._observations.pop(0) if len(self._observations) > 1 else self._observations[0]


class _LabelVision(_Vision):
    """A vision model that can also read shipping labels."""

    def __init__(self, label: PackageLabel | Exception | None, *observations) -> None:
        super().__init__(*(observations or (_THERE,)))
        self._label = label
        self.label_images: list[bytes] = []

    def read_package_label(self, image: bytes) -> PackageLabel | None:
        self.label_images.append(image)
        if isinstance(self._label, Exception):
            raise self._label
        return self._label


_UPS = PackageLabel(carrier="UPS", recipient_name="Jon", tracking_number="1Z999AA10123456784")


def _event(kind: str, event_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=event_id, type="smartDetectZone", device_id="cam1", smart_detect_types=(kind,)
    )


class _Harness:
    def __init__(self, tmp_path: Path, vision=None, media=True, **config) -> None:
        self.client = MagicMock()
        self.client.get_public_api_camera_snapshot = AsyncMock(
            side_effect=lambda device_id, package=False: b"package-lens" if package else b"visitor"
        )
        camera = MagicMock()
        camera.rtsps_streams.get_stream_url.return_value = "rtsps://console/cam1"
        self.client.public_bootstrap.cameras = {"cam1": camera}
        self.notifier = MagicMock()
        self.log = JSONLPackageLog(tmp_path / "package_events.jsonl")
        self.snapshots = SnapshotStore(tmp_path / "snapshots")
        self.media_dir = tmp_path / "media" if media else None
        self.clips: list[Path] = []

        def fake_clip(url: str, path: Path, seconds: float, verify_ssl: bool) -> None:
            path.write_bytes(b"fake-mp4")
            self.clips.append(path)

        self.bridge = UnifiBridge(
            config=UnifiConfig(
                host="127.0.0.1",
                port=443,
                api_key="k",
                ring_chime_delay_seconds=0.0,
                **config,
            ),
            session_store=JSONFileSessionStore(tmp_path / "sessions"),
            client=self.client,
            ha_notifier=self.notifier,
            package_log=self.log,
            snapshot_store=self.snapshots,
            public_base_url="https://knock.example.com",
            vision_provider=vision,
            package_media_dir=self.media_dir,
            clip_recorder=fake_clip,
        )

    def kinds(self) -> list[str]:
        return [event.kind for event in reversed(self.log.recent())]

    def messages(self) -> list[str]:
        return [call.args[0] for call in self.notifier.notify.call_args_list]

    async def deliver(self) -> None:
        """A box lands with nobody around -> notified, now pending."""
        await self.bridge.handle_event(_event("package", "pkg1"))

    async def someone_comes_and_goes(self, event_id: str = "p1") -> None:
        self.bridge._on_event(_event("person", event_id), EventChange.STARTED)
        await _settle()
        self.bridge._on_event(_event("person", event_id), EventChange.ENDED)
        await _settle()


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)
        current = asyncio.current_task()
        pending = [t for t in asyncio.all_tasks() if t is not current and not t.done()]
        if not pending:
            return
        await asyncio.gather(*pending)


@pytest.fixture(autouse=True)
def _no_recheck_delay(monkeypatch) -> None:
    monkeypatch.setattr(package_alerts_module, "_REMOVAL_RECHECK_SECONDS", 0.0)


# -- the shipping label --------------------------------------------------------------


def test_the_alert_leads_with_the_label_read_off_its_own_photo(tmp_path) -> None:
    vision = _LabelVision(_UPS)
    h = _Harness(tmp_path, vision=vision)

    asyncio.run(h.deliver())

    assert h.messages() == [
        "A UPS package for Jon (tracking ...6784) was detected at the door (camera: cam1)."
    ]
    assert vision.label_images == [b"package-lens"]
    image_url = h.notifier.notify.call_args.kwargs["image"]
    assert h.snapshots.take(image_url.rsplit("/", 1)[1]) == b"package-lens"
    # One package-camera fetch serves both the photo and the label.
    assert h.client.get_public_api_camera_snapshot.await_count == 1


def test_a_carrier_starting_with_a_vowel_gets_an(tmp_path) -> None:
    h = _Harness(tmp_path, vision=_LabelVision(PackageLabel(carrier="Amazon")))

    asyncio.run(h.deliver())

    assert h.messages() == ["An Amazon package was detected at the door (camera: cam1)."]


@pytest.mark.parametrize(
    "label",
    [None, PackageLabel(recipient_name="Jon"), RuntimeError("ollama down")],
    ids=["nothing-legible", "no-carrier", "read-failed"],
)
def test_without_a_usable_label_the_alert_is_unchanged(tmp_path, label) -> None:
    h = _Harness(tmp_path, vision=_LabelVision(label))

    asyncio.run(h.deliver())

    assert h.messages() == ["A package was detected at the door (camera: cam1)."]
    assert h.notifier.notify.call_args.kwargs["image"] is not None
    assert h.kinds() == ["detected", "notified"]


def test_a_failed_package_snapshot_still_sends_the_alert(tmp_path) -> None:
    h = _Harness(tmp_path, vision=_LabelVision(_UPS))
    h.client.get_public_api_camera_snapshot.side_effect = RuntimeError("camera offline")

    asyncio.run(h.deliver())

    assert h.messages() == ["A package was detected at the door (camera: cam1)."]
    assert h.notifier.notify.call_args.kwargs["image"] is None


def test_reminders_and_removals_name_the_labelled_package(tmp_path) -> None:
    h = _Harness(
        tmp_path,
        vision=_LabelVision(_UPS, _THERE, _GONE),
        package_reminder_hours=1e-6,
        package_clip_seconds=0,
    )

    async def scenario() -> None:
        await h.deliver()
        await _settle()

    asyncio.run(scenario())

    assert h.kinds() == ["detected", "notified", "reminded", "removed"]
    reminder, removal = h.messages()[1:]
    assert reminder.startswith(
        "Reminder: a UPS package for Jon (tracking ...6784) has been at the door"
    )
    assert removal == (
        "A UPS package for Jon (tracking ...6784) is no longer at the door, and nobody"
        " marked it as received (camera: cam1)."
    )


# -- reminders ---------------------------------------------------------------------


def test_reminders_are_off_by_default(tmp_path) -> None:
    h = _Harness(tmp_path)

    async def scenario() -> None:
        await h.deliver()
        await _settle()

    asyncio.run(scenario())

    assert h.kinds() == ["detected", "notified"]


def test_reminders_repeat_up_to_the_cap_replacing_the_same_notification(tmp_path) -> None:
    h = _Harness(tmp_path, package_reminder_hours=1e-6)

    async def scenario() -> None:
        await h.deliver()
        await _settle()

    asyncio.run(scenario())

    assert h.kinds() == ["detected", "notified", "reminded", "reminded", "reminded"]
    reminders = h.messages()[1:]
    assert len(reminders) == 3
    assert all("hasn't been marked as received" in m for m in reminders)
    tags = {call.kwargs["tag"] for call in h.notifier.notify.call_args_list}
    assert tags == {"knock-package-cam1"}


def test_got_it_stops_reminders(tmp_path) -> None:
    h = _Harness(tmp_path, package_reminder_hours=1.0)

    async def scenario() -> None:
        await h.deliver()
        await h.bridge.handle_notification_action("knock_package_ack", "cam1")
        await _settle()

    asyncio.run(scenario())

    assert h.kinds() == ["detected", "notified", "acknowledged"]
    h.notifier.clear.assert_called_once_with("knock-package-cam1")


def test_reminder_reports_removal_instead_when_the_box_is_already_gone(tmp_path) -> None:
    h = _Harness(tmp_path, vision=_Vision(_GONE), package_reminder_hours=1e-6)

    async def scenario() -> None:
        await h.deliver()
        await _settle()

    asyncio.run(scenario())

    assert h.kinds() == ["detected", "notified", "removed"]
    assert "no longer at the door" in h.messages()[-1]


# -- removal watch -------------------------------------------------------------------


def test_box_gone_after_someone_leaves_alerts_with_their_photo_and_saves_it(tmp_path) -> None:
    h = _Harness(tmp_path, vision=_Vision(_GONE), package_clip_seconds=0)

    async def scenario() -> None:
        await h.deliver()
        await h.someone_comes_and_goes()

    asyncio.run(scenario())

    assert h.kinds() == ["detected", "notified", "removed"]
    message = h.messages()[-1]
    assert "nobody marked it as received" in message
    assert "who came by" in message
    # The attached photo is the main-camera still of the visitor.
    image_url = h.notifier.notify.call_args.kwargs["image"]
    assert h.snapshots.take(image_url.rsplit("/", 1)[1]) == b"visitor"
    # Both stills are kept on disk and named in the log.
    media = h.log.recent()[0].media
    assert [name.split("-", 2)[2] for name in media] == [
        "visitor-1.jpg",
        "porch-after.jpg",
    ]
    assert h.media_dir is not None
    assert (h.media_dir / media[0]).read_bytes() == b"visitor"
    assert (h.media_dir / media[1]).read_bytes() == b"package-lens"


def test_box_gone_also_keeps_the_clip(tmp_path) -> None:
    h = _Harness(tmp_path, vision=_Vision(_GONE))

    async def scenario() -> None:
        await h.deliver()
        await h.someone_comes_and_goes()

    asyncio.run(scenario())

    media = h.log.recent()[0].media
    assert len(h.clips) == 1
    assert h.clips[0].name in media
    assert h.clips[0].exists()


def test_a_passer_by_leaves_no_trace(tmp_path) -> None:
    h = _Harness(tmp_path, vision=_Vision(_THERE))

    async def scenario() -> None:
        await h.deliver()
        await h.someone_comes_and_goes()

    asyncio.run(scenario())

    assert h.kinds() == ["detected", "notified"]
    assert len(h.messages()) == 1
    # Their clip was recorded but thrown away once the box was still there.
    assert len(h.clips) == 1
    assert not h.clips[0].exists()


def test_one_missed_look_is_not_a_removal(tmp_path) -> None:
    # First look misses the box (shadow, odd angle); the recheck sees it.
    vision = _Vision(_GONE, _THERE)
    h = _Harness(tmp_path, vision=vision)

    async def scenario() -> None:
        await h.deliver()
        await h.someone_comes_and_goes()

    asyncio.run(scenario())

    assert vision.looks == 2
    assert h.kinds() == ["detected", "notified"]


def test_an_unsure_vision_model_never_reads_as_gone(tmp_path) -> None:
    h = _Harness(tmp_path, vision=_Vision(SceneObservation(summary="Hard to tell.")))

    async def scenario() -> None:
        await h.deliver()
        await h.someone_comes_and_goes()

    asyncio.run(scenario())

    assert h.kinds() == ["detected", "notified"]


def test_removal_watch_needs_a_vision_model(tmp_path) -> None:
    h = _Harness(tmp_path, vision=None)

    async def scenario() -> None:
        await h.deliver()
        await h.someone_comes_and_goes()

    asyncio.run(scenario())

    assert h.kinds() == ["detected", "notified"]
    assert h.clips == []


def test_removal_watch_can_be_turned_off(tmp_path) -> None:
    vision = _Vision(_GONE)
    h = _Harness(tmp_path, vision=vision, package_removal_alerts=False)

    async def scenario() -> None:
        await h.deliver()
        await h.someone_comes_and_goes()

    asyncio.run(scenario())

    assert vision.looks == 0
    assert h.kinds() == ["detected", "notified"]


def test_nothing_is_watched_after_got_it(tmp_path) -> None:
    vision = _Vision(_GONE)
    h = _Harness(tmp_path, vision=vision)

    async def scenario() -> None:
        await h.deliver()
        await h.bridge.handle_notification_action("knock_package_ack", "cam1")
        await h.someone_comes_and_goes()

    asyncio.run(scenario())

    assert vision.looks == 0
    assert h.kinds() == ["detected", "notified", "acknowledged"]


def test_without_a_media_folder_the_alert_still_carries_the_photo(tmp_path) -> None:
    h = _Harness(tmp_path, vision=_Vision(_GONE), media=False)

    async def scenario() -> None:
        await h.deliver()
        await h.someone_comes_and_goes()

    asyncio.run(scenario())

    assert h.kinds()[-1] == "removed"
    assert h.log.recent()[0].media == []
    assert h.notifier.notify.call_args.kwargs["image"] is not None
    assert h.clips == []


def test_the_delivery_drivers_own_departure_does_not_trigger_a_removal_check(tmp_path) -> None:
    vision = _Vision(_GONE)
    h = _Harness(tmp_path, vision=vision)

    async def scenario() -> None:
        h.bridge._on_event(_event("person", "driver"), EventChange.STARTED)
        await h.bridge.handle_event(_event("package", "pkg1"))
        h.bridge._on_event(_event("person", "driver"), EventChange.ENDED)
        await _settle()

    asyncio.run(scenario())

    assert vision.looks == 0
    assert h.kinds() == ["detected", "notified"]


def test_a_visitor_who_rings_first_still_gets_photographed(tmp_path) -> None:
    h = _Harness(tmp_path, vision=_Vision(_GONE), trigger_on=["ring"], package_clip_seconds=0)

    async def scenario() -> None:
        await h.deliver()
        h.bridge.packages.conversation_started("cam1")
        h.bridge._on_event(_event("person", "p1"), EventChange.STARTED)
        await _settle()
        h.bridge._on_event(_event("person", "p1"), EventChange.ENDED)
        h.bridge.packages.conversation_ended("cam1")
        await _settle()

    asyncio.run(scenario())

    assert h.kinds() == ["detected", "notified", "removed"]
    assert "who came by" in h.messages()[-1]


def test_two_departure_signals_share_one_removal_check(tmp_path) -> None:
    vision = _Vision(_GONE)
    h = _Harness(tmp_path, vision=vision, package_clip_seconds=0)

    async def scenario() -> None:
        await h.deliver()
        h.bridge._on_event(_event("person", "p1"), EventChange.STARTED)
        await _settle()
        # Person ENDED with no conversation running starts a check; a second
        # departure signal right behind it must not start another one.
        h.bridge._on_event(_event("person", "p1"), EventChange.ENDED)
        h.bridge.packages.conversation_started("cam1")
        h.bridge.packages.conversation_ended("cam1")
        await _settle()

    asyncio.run(scenario())

    assert h.kinds().count("removed") == 1
    assert vision.looks == 2  # one look + one recheck, not two of each


# -- Protect video export (username/password configured) ------------------------


def _with_export(h: _Harness, *, fails: bool = False) -> AsyncMock:
    async def export(camera_id, start, end, *, channel_index, validate_channel_id, output_file):
        if fails:
            raise RuntimeError("export refused")
        output_file.write_bytes(b"protect-mp4")

    h.client.get_camera_video = AsyncMock(side_effect=export)
    return h.client.get_camera_video


def test_with_a_login_the_clip_comes_from_protect_with_preroll(tmp_path) -> None:
    h = _Harness(tmp_path, vision=_Vision(_GONE), username="knock", password="pw")
    export = _with_export(h)

    async def scenario() -> None:
        await h.deliver()
        await h.someone_comes_and_goes()

    asyncio.run(scenario())

    export.assert_awaited_once()
    camera_id, start, end = export.call_args.args
    assert camera_id == "cam1"
    assert (end - start).total_seconds() >= 10.0  # reaches back before detection
    assert export.call_args.kwargs["channel_index"] == 0
    assert export.call_args.kwargs["validate_channel_id"] is False
    media = h.log.recent()[0].media
    protect_clips = [name for name in media if name.endswith("protect-clip.mp4")]
    assert len(protect_clips) == 1
    assert h.media_dir is not None
    assert (h.media_dir / protect_clips[0]).read_bytes() == b"protect-mp4"
    # The self-recorded fallback was replaced, not kept alongside.
    assert not h.clips[0].exists()
    assert h.clips[0].name not in media


def test_a_failed_export_keeps_the_self_recorded_clip(tmp_path) -> None:
    h = _Harness(tmp_path, vision=_Vision(_GONE), username="knock", password="pw")
    _with_export(h, fails=True)

    async def scenario() -> None:
        await h.deliver()
        await h.someone_comes_and_goes()

    asyncio.run(scenario())

    media = h.log.recent()[0].media
    assert h.clips[0].name in media
    assert h.clips[0].exists()
    assert not any(name.endswith("protect-clip.mp4") for name in media)


def test_without_a_login_protect_export_is_never_attempted(tmp_path) -> None:
    h = _Harness(tmp_path, vision=_Vision(_GONE))
    export = _with_export(h)

    async def scenario() -> None:
        await h.deliver()
        await h.someone_comes_and_goes()

    asyncio.run(scenario())

    export.assert_not_awaited()


def test_half_a_login_counts_as_none(tmp_path) -> None:
    h = _Harness(tmp_path, username="knock")
    assert h.bridge.has_private_api is False


# -- the clip recorder itself ----------------------------------------------------------


def test_record_rtsp_clip_remuxes_video_without_reencoding(tmp_path) -> None:
    source = tmp_path / "source.mp4"
    with av.open(str(source), "w") as container:
        stream = container.add_stream("mpeg4", rate=10)
        stream.width, stream.height, stream.pix_fmt = 64, 48, "yuv420p"
        stream.time_base = Fraction(1, 10)
        for index in range(20):
            frame = av.VideoFrame(64, 48, "yuv420p")
            frame.pts = index
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)

    clip = tmp_path / "clip.mp4"
    record_rtsp_clip(str(source), clip, seconds=30.0)

    with av.open(str(clip)) as result:
        video = result.streams.video[0]
        assert video.codec_context.name == "mpeg4"
        assert sum(1 for _ in result.demux(video) if _.size) == 20


def test_record_rtsp_clip_rejects_a_stream_without_video(tmp_path) -> None:
    source = tmp_path / "audio.wav"
    with av.open(str(source), "w") as container:
        stream = container.add_stream("pcm_s16le", rate=8000)
        frame = av.AudioFrame(format="s16", layout="mono", samples=800)
        frame.sample_rate = 8000
        for plane in frame.planes:
            plane.update(bytes(plane.buffer_size))
        for packet in stream.encode(frame):
            container.mux(packet)

    with pytest.raises(RuntimeError, match="no video track"):
        record_rtsp_clip(str(source), tmp_path / "clip.mp4", seconds=1.0)


# -- config -----------------------------------------------------------------------------


def test_package_followup_defaults() -> None:
    config = UnifiConfig()
    assert config.package_reminder_hours == 0.0
    assert config.package_removal_alerts is True
    assert config.package_clip_seconds == 30.0


def test_package_followup_from_env(monkeypatch) -> None:
    monkeypatch.setenv("KNOCK_UNIFI_PACKAGE_REMINDER_HOURS", "4")
    monkeypatch.setenv("KNOCK_UNIFI_PACKAGE_REMOVAL_ALERTS", "no")
    monkeypatch.setenv("KNOCK_UNIFI_PACKAGE_CLIP_SECONDS", "0")

    config = UnifiConfig.from_env()

    assert config.package_reminder_hours == 4.0
    assert config.package_removal_alerts is False
    assert config.package_clip_seconds == 0.0


def test_login_settings_from_env(monkeypatch) -> None:
    monkeypatch.setenv("KNOCK_UNIFI_USERNAME", "knock")
    monkeypatch.setenv("KNOCK_UNIFI_PASSWORD", "hunter2")
    monkeypatch.setenv("KNOCK_UNIFI_PACKAGE_CLIP_PREROLL_SECONDS", "5")

    config = UnifiConfig.from_env()

    assert config.username == "knock"
    assert config.password.get_secret_value() == "hunter2"
    assert "hunter2" not in repr(config)
    assert config.package_clip_preroll_seconds == 5.0


def test_bridge_logs_in_with_both_api_key_and_password(monkeypatch, tmp_path) -> None:
    created: list[dict] = []

    def fake_client(**kwargs) -> MagicMock:
        created.append(kwargs)
        return MagicMock()

    monkeypatch.setattr(unifi_module, "ProtectApiClient", fake_client)

    UnifiBridge(
        config=UnifiConfig(api_key="key", username="knock", password="pw"),
        session_store=JSONFileSessionStore(tmp_path),
    )
    UnifiBridge(config=UnifiConfig(api_key="key"), session_store=JSONFileSessionStore(tmp_path))

    assert created[0]["api_key"] == "key"
    assert (created[0]["username"], created[0]["password"]) == ("knock", "pw")
    # API key only: no login passed at all (uiprotect stays public-only).
    assert (created[1]["username"], created[1]["password"]) == (None, None)
