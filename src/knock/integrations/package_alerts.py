"""Package alerts for a doorbell with a package camera (UniFi Protect).

Owned by `UnifiBridge` (split out of it for size, not reuse -- it reaches
back into the bridge for the Protect client, the notifier and the snapshot
store, read at call time so tests can swap them after construction).

Lifecycle of one package, per camera:

1. **Detected.** Protect's own package smart-detect fires. Never a
   conversation -- there's nobody to talk to.
2. **Notified.** A "package"-category notification with the package-camera
   photo and a "Got it" button. Held back while a visitor is still at the
   door (a running conversation, or an open person detection on that
   camera) and sent the moment they walk away.
3. **Pending** until someone taps "Got it". While pending:
   - **Reminders** (`package_reminder_hours`, off by default) re-send the
     notification -- same tag, so it replaces the old one rather than
     stacking -- up to `_MAX_REMINDERS` times.
   - **Removal watch** (`package_removal_alerts`, needs a vision provider):
     whenever someone comes and goes, KNOCK grabs main-camera stills of
     them on arrival (and optionally records a short clip off the RTSPS
     stream, `package_clip_seconds`), then looks through the package
     camera once they've left. If the box is gone (two looks, a few
     seconds apart, both agree), that's "taken without anyone tapping Got
     it": a notification with the photo of whoever came by, and the
     stills/clip saved under the package media folder. If the box is still
     there, it was just a passer-by and the stills/clip are thrown away.
4. **Acknowledged** ("Got it") -- clears the notification from every phone
   and ends the pending state. Or **removed**, per above.

Wording stays neutral throughout ("no longer on the porch," never
"stolen") -- the most likely taker is someone who lives there.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import av
from uiprotect import EventChange

from knock.config import UnifiConfig
from knock.core.package_log import PackageEvent, PackageEventKind
from knock.integrations.homeassistant import (
    ACTION_DEVICE_ID_SEP,
    KNOCK_PACKAGE_ACK_ACTION,
    package_notification_tag,
)
from knock.providers.vision.base import observe_scene

if TYPE_CHECKING:
    from knock.integrations.unifi import UnifiBridge

logger = logging.getLogger(__name__)

_SMART_DETECT_EVENT_TYPES = {"smartDetectZone", "smartDetectLine", "smartDetectLoiterZone"}

# An open person detection older than this no longer counts as "someone's at
# the door" -- otherwise one lost ENDED event would hold every later package
# alert on that camera for its full departure timeout, forever.
_PERSON_EVENT_MAX_AGE_SECONDS = 600.0

# Reminders stop after this many -- a box nobody's collecting after three
# nags isn't going to be collected because of a fourth.
_MAX_REMINDERS = 3

# Removal is only called after two looks through the package camera this far
# apart both say the box is gone -- one frame can catch it mid-pickup by
# someone just moving it, or the vision model can miss it in a shadow once.
_REMOVAL_RECHECK_SECONDS = 10.0

# Main-camera stills kept per pending package between "someone arrived" and
# "they left" -- enough to show who came by without hoarding a wall of them.
_MAX_EVIDENCE_STILLS = 3

# How long a Protect video export may take before falling back to KNOCK's own
# recording -- a short clip normally comes back in a few seconds.
_EXPORT_TIMEOUT_SECONDS = 60.0

DEFAULT_PACKAGE_MEDIA_DIR_ENV_VAR = "KNOCK_PACKAGE_MEDIA_DIR"


def default_package_media_dir() -> Path:
    configured = os.environ.get(DEFAULT_PACKAGE_MEDIA_DIR_ENV_VAR)
    if configured:
        return Path(configured)
    return Path.home() / ".local" / "share" / "knock" / "package_media"


def record_rtsp_clip(rtsp_url: str, path: Path, seconds: float, verify_ssl: bool = True) -> None:
    """Blocking: copy `seconds` of the stream's video track into an MP4 at
    `path` -- a remux (packets copied as-is), not a re-encode, so it costs
    almost nothing beyond the network read. Runs in a worker thread, same
    as `_capture_rtsp_audio`.
    """
    options = {"rtsp_transport": "tcp"}
    if not verify_ssl:
        # Same self-signed-cert reasoning as `_capture_rtsp_audio`.
        options["tls_verify"] = "0"
    with av.open(rtsp_url, timeout=(5.0, 5.0), options=options) as source:
        if not source.streams.video:
            raise RuntimeError(f"RTSP stream has no video track: {rtsp_url}")
        video_in = source.streams.video[0]
        with av.open(str(path), "w", format="mp4") as output:
            video_out = output.add_stream_from_template(video_in)
            start = time.monotonic()
            for packet in source.demux(video_in):
                # Demux ends with a flush packet that carries no data.
                if packet.dts is None:
                    continue
                packet.stream = video_out
                output.mux(packet)
                if time.monotonic() - start >= seconds:
                    break


class _ProtectEventLike(Protocol):
    @property
    def id(self) -> str: ...

    @property
    def type(self) -> Any: ...

    @property
    def device_id(self) -> str: ...

    @property
    def smart_detect_types(self) -> Any: ...


@dataclass
class _PendingPackage:
    """A notified package nobody has tapped "Got it" on yet."""

    notified_at: float
    reminders_sent: int = 0
    # Main-camera stills of whoever has come by since the last removal check.
    evidence: list[bytes] = field(default_factory=list)
    # When the first of them walked up -- where a Protect export starts.
    visitor_arrived_at: datetime | None = None
    clip_path: Path | None = None
    clip_task: asyncio.Task[None] | None = None
    reminder_task: asyncio.Task[None] | None = None
    # A removal check is already looking -- a person ENDED and a
    # conversation ending can both signal the same departure.
    checking: bool = False


class PackageAlerts:
    def __init__(self, bridge: UnifiBridge) -> None:
        self._bridge = bridge
        # camera -> {person event id: monotonic start time}
        self._open_person_events: dict[str, dict[str, float]] = {}
        self._active_conversations: dict[str, int] = {}
        # A package alert waiting for the visitor to leave, per camera.
        self._departures: dict[str, asyncio.Event] = {}
        self._last_detection: dict[str, float] = {}
        self._pending: dict[str, _PendingPackage] = {}
        # Strong refs so fire-and-forget tasks aren't garbage-collected.
        self._tasks: set[asyncio.Task[Any]] = set()

    @property
    def _config(self) -> UnifiConfig:
        return self._bridge.config

    def _spawn(self, coro: Any) -> asyncio.Task[Any]:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # -- who's at the door --------------------------------------------------------

    def is_package_detection(self, event: _ProtectEventLike) -> bool:
        if not self._config.package_alerts:
            return False
        camera_ids = self._config.trigger_camera_ids
        if camera_ids and event.device_id not in camera_ids:
            return False
        return str(event.type) in _SMART_DETECT_EVENT_TYPES and "package" in {
            str(t) for t in event.smart_detect_types
        }

    def visitor_present(self, device_id: str) -> bool:
        """Someone's still at the door: a conversation is running, or
        Protect has an open (not yet ENDED) person detection on this camera.
        Person detections are tracked whether or not "person" is in
        `trigger_on` -- a driver who drops a box without ringing never
        starts a conversation, but is still standing there.
        """
        open_events = self._open_person_events.get(device_id, {})
        cutoff = time.monotonic() - _PERSON_EVENT_MAX_AGE_SECONDS
        for event_id in [eid for eid, started in open_events.items() if started < cutoff]:
            del open_events[event_id]
        return bool(open_events) or self._active_conversations.get(device_id, 0) > 0

    def track_presence(self, event: _ProtectEventLike, change: EventChange) -> None:
        if "person" not in {str(t) for t in event.smart_detect_types}:
            return
        device_id = event.device_id
        open_events = self._open_person_events.setdefault(device_id, {})
        if change == EventChange.STARTED:
            # The first person in frame -- whether or not a ring already
            # started a conversation a moment earlier.
            self.visitor_present(device_id)  # prunes stale entries
            arriving = not open_events
            open_events[event.id] = time.monotonic()
            if arriving and device_id in self._pending:
                self._spawn(self._gather_evidence(device_id))
        elif change in (EventChange.ENDED, EventChange.REMOVED):
            open_events.pop(event.id, None)
            self._maybe_departed(device_id)

    def conversation_started(self, device_id: str) -> None:
        self._active_conversations[device_id] = self._active_conversations.get(device_id, 0) + 1

    def conversation_ended(self, device_id: str) -> None:
        self._active_conversations[device_id] -= 1
        self._maybe_departed(device_id)

    def _maybe_departed(self, device_id: str) -> None:
        if self.visitor_present(device_id):
            return
        departed = self._departures.get(device_id)
        if departed is not None:
            departed.set()
        elif device_id in self._pending and self._removal_watch_enabled():
            self._spawn(self._check_for_removal(device_id))

    # -- detection -> notification -------------------------------------------------

    async def on_package_detected(self, device_id: str) -> None:
        now = time.monotonic()
        last = self._last_detection.get(device_id)
        if device_id in self._departures or (
            last is not None and now - last < self._config.package_cooldown_seconds
        ):
            logger.info("Package on %s already reported recently, skipping", device_id)
            return
        self._last_detection[device_id] = now

        present = self.visitor_present(device_id)
        self._record(device_id, "detected", visitor_present=present)
        if not present:
            await self._send_alert(device_id, left_by_visitor=False)
            return

        departed = asyncio.Event()
        self._departures[device_id] = departed
        self._spawn(self._alert_after_departure(device_id, departed))

    async def _alert_after_departure(self, device_id: str, departed: asyncio.Event) -> None:
        try:
            await asyncio.wait_for(
                departed.wait(), timeout=self._config.package_departure_timeout_seconds
            )
        except TimeoutError:
            logger.info("Visitor at %s never seen leaving, sending package alert anyway", device_id)
        finally:
            if self._departures.get(device_id) is departed:
                del self._departures[device_id]
        await self._send_alert(device_id, left_by_visitor=True)

    async def _send_alert(self, device_id: str, *, left_by_visitor: bool) -> None:
        message = (
            f"A package was left at the door (camera: {device_id})."
            if left_by_visitor
            else f"A package was detected at the door (camera: {device_id})."
        )
        image_url = await self._bridge._build_snapshot_url(device_id, package=True)
        await self._notify(device_id, message, image_url)
        self._record(device_id, "notified")
        self._start_pending(device_id)

    async def _notify(self, device_id: str, message: str, image_url: str | None) -> None:
        ack = f"{KNOCK_PACKAGE_ACK_ACTION}{ACTION_DEVICE_ID_SEP}{device_id}"
        await self._bridge._notify_household(
            device_id,
            message,
            actions=[{"action": ack, "title": "Got it"}],
            category="package",
            image_url=image_url,
            tag=package_notification_tag(device_id),
        )

    # -- pending: reminders + removal watch --------------------------------------

    def _start_pending(self, device_id: str) -> None:
        # A second box before the first was acknowledged keeps one pending
        # record per camera, matching the one notification per camera.
        previous = self._pending.pop(device_id, None)
        if previous is not None:
            self._discard(previous)
        pending = _PendingPackage(notified_at=time.monotonic())
        self._pending[device_id] = pending
        if self._config.package_reminder_hours > 0:
            pending.reminder_task = self._spawn(self._remind(device_id, pending))

    def _end_pending(self, device_id: str) -> _PendingPackage | None:
        pending = self._pending.pop(device_id, None)
        if pending is not None and pending.reminder_task is not None:
            if pending.reminder_task is not asyncio.current_task():
                pending.reminder_task.cancel()
        return pending

    def _removal_watch_enabled(self) -> bool:
        return self._config.package_removal_alerts and self._bridge.vision_provider is not None

    async def _remind(self, device_id: str, pending: _PendingPackage) -> None:
        interval = self._config.package_reminder_hours * 3600
        while pending.reminders_sent < _MAX_REMINDERS:
            await asyncio.sleep(interval)
            if self._pending.get(device_id) is not pending:
                return
            # With a vision model, don't nag about a box that's already gone:
            # a removal nobody saw happen (no person event) still counts.
            if self._removal_watch_enabled() and await self._package_gone(device_id):
                await self._report_removal(device_id, pending)
                return
            pending.reminders_sent += 1
            hours = (time.monotonic() - pending.notified_at) / 3600
            message = (
                f"Reminder: a package has been at the door for about {hours:.0f} hour"
                f"{'' if round(hours) == 1 else 's'} and hasn't been marked as received "
                f"(camera: {device_id})."
            )
            image_url = await self._bridge._build_snapshot_url(device_id, package=True)
            await self._notify(device_id, message, image_url)
            self._record(device_id, "reminded")

    async def _gather_evidence(self, device_id: str) -> None:
        """Someone just walked up while a package is pending: grab a still of
        them, and start a clip if configured. Kept only if the box turns out
        to be gone once they've left (see `_check_for_removal`).
        """
        pending = self._pending.get(device_id)
        if pending is None or not self._removal_watch_enabled():
            return
        if pending.visitor_arrived_at is None:
            pending.visitor_arrived_at = datetime.now(UTC)
        media_dir = self._bridge.package_media_dir
        if (
            media_dir is not None
            and self._config.package_clip_seconds > 0
            and pending.clip_task is None
        ):
            url = self._bridge._rtsp_url(device_id)
            if url:
                stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
                media_dir.mkdir(parents=True, exist_ok=True)
                pending.clip_path = media_dir / f"{stamp}-{device_id}-clip.mp4"
                pending.clip_task = self._spawn(
                    self._record_clip(url, pending.clip_path, self._config.package_clip_seconds)
                )
        try:
            still = await self._bridge.client.get_public_api_camera_snapshot(device_id)
        except Exception as exc:  # noqa: BLE001 - evidence is best-effort
            logger.info("Evidence snapshot for %s failed: %s", device_id, exc)
            return
        if still is not None and len(pending.evidence) < _MAX_EVIDENCE_STILLS:
            pending.evidence.append(still)

    async def _record_clip(self, url: str, path: Path, seconds: float) -> None:
        try:
            await asyncio.to_thread(
                self._bridge._clip_recorder, url, path, seconds, self._config.verify_ssl
            )
        except Exception as exc:  # noqa: BLE001 - a clip is a bonus
            logger.info("Clip recording to %s failed: %s", path, exc)
            path.unlink(missing_ok=True)

    async def _package_gone(self, device_id: str) -> bool:
        """True only when the vision model positively says no package is
        visible -- a failed snapshot or an unsure model never reads as gone.
        """
        vision_provider = self._bridge.vision_provider
        if vision_provider is None:
            return False
        try:
            snapshot = await self._bridge.client.get_public_api_camera_snapshot(
                device_id, package=True
            )
            if snapshot is None:
                return False
            observation = await asyncio.to_thread(observe_scene, vision_provider, snapshot)
        except Exception as exc:  # noqa: BLE001 - unsure means "still there"
            logger.info("Package-camera check for %s failed: %s", device_id, exc)
            return False
        return observation.package_visible is False

    async def _check_for_removal(self, device_id: str) -> None:
        pending = self._pending.get(device_id)
        if pending is None or pending.checking:
            return
        pending.checking = True
        try:
            gone = await self._package_gone(device_id)
            if gone:
                await asyncio.sleep(_REMOVAL_RECHECK_SECONDS)
                gone = await self._package_gone(device_id)
        finally:
            pending.checking = False
        if self._pending.get(device_id) is not pending:
            return  # acknowledged (or replaced) while we were looking
        if gone:
            await self._report_removal(device_id, pending)
        else:
            # Just someone passing by -- don't keep their picture.
            self._discard(pending, keep_pending=True)

    async def _report_removal(self, device_id: str, pending: _PendingPackage) -> None:
        self._end_pending(device_id)
        if pending.clip_task is not None:
            # Let a clip that's still recording finish -- it's the evidence.
            await asyncio.wait({pending.clip_task}, timeout=self._config.package_clip_seconds + 15)
        await self._export_protect_clip(device_id, pending)

        try:
            after = await self._bridge.client.get_public_api_camera_snapshot(
                device_id, package=True
            )
        except Exception:  # noqa: BLE001 - photo is best-effort
            after = None
        media = self._save_media(device_id, pending, after)

        photo = pending.evidence[0] if pending.evidence else after
        image_url = self._bridge._snapshot_url_for(photo) if photo is not None else None
        message = (
            "A package is no longer at the door, and nobody marked it as received"
            f" (camera: {device_id})."
        )
        if pending.evidence:
            message += " The photo shows who came by."
        await self._notify(device_id, message, image_url)
        self._record(device_id, "removed", media=media)

    async def _export_protect_clip(self, device_id: str, pending: _PendingPackage) -> None:
        """With a full login, swap the self-recorded clip for Protect's own
        recording, which reaches back before the person was even detected.
        The self-recorded clip stays as the fallback if the export fails.
        """
        media_dir = self._bridge.package_media_dir
        if (
            not self._bridge.has_private_api
            or media_dir is None
            or pending.visitor_arrived_at is None
            or self._config.package_clip_seconds <= 0
        ):
            return
        start = pending.visitor_arrived_at - timedelta(
            seconds=self._config.package_clip_preroll_seconds
        )
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        path = media_dir / f"{stamp}-{device_id}-protect-clip.mp4"
        try:
            media_dir.mkdir(parents=True, exist_ok=True)
            await asyncio.wait_for(
                self._bridge.client.get_camera_video(
                    device_id,
                    start,
                    datetime.now(UTC),
                    # Main lens; the private bootstrap that channel
                    # validation reads is never loaded in API-key mode.
                    channel_index=0,
                    validate_channel_id=False,
                    output_file=path,
                ),
                timeout=_EXPORT_TIMEOUT_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001 - fall back to the own recording
            logger.info("Protect clip export for %s failed: %s", device_id, exc)
            path.unlink(missing_ok=True)
            return
        if not path.exists() or path.stat().st_size == 0:
            path.unlink(missing_ok=True)
            return
        if pending.clip_path is not None:
            pending.clip_path.unlink(missing_ok=True)
        pending.clip_path = path

    def _save_media(
        self, device_id: str, pending: _PendingPackage, after: bytes | None
    ) -> list[str]:
        media_dir = self._bridge.package_media_dir
        names: list[str] = []
        if media_dir is None:
            return names
        try:
            media_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            for index, still in enumerate(pending.evidence, start=1):
                name = f"{stamp}-{device_id}-visitor-{index}.jpg"
                (media_dir / name).write_bytes(still)
                names.append(name)
            if after is not None:
                name = f"{stamp}-{device_id}-porch-after.jpg"
                (media_dir / name).write_bytes(after)
                names.append(name)
        except OSError as exc:
            logger.warning("Saving package media for %s failed: %s", device_id, exc)
        clip = pending.clip_path
        if clip is not None and clip.exists() and clip.stat().st_size > 0:
            names.append(clip.name)
        return names

    def _discard(self, pending: _PendingPackage, *, keep_pending: bool = False) -> None:
        """Throw away evidence that turned out not to matter."""
        pending.evidence.clear()
        pending.visitor_arrived_at = None
        clip_task, clip_path = pending.clip_task, pending.clip_path
        pending.clip_task = pending.clip_path = None
        if clip_path is None:
            return
        if clip_task is not None and not clip_task.done():
            clip_task.add_done_callback(lambda _: clip_path.unlink(missing_ok=True))
        else:
            clip_path.unlink(missing_ok=True)
        if not keep_pending and pending.reminder_task is not None:
            pending.reminder_task.cancel()

    # -- "Got it" -------------------------------------------------------------------

    async def acknowledge(self, device_id: str) -> None:
        pending = self._end_pending(device_id)
        if pending is not None:
            self._discard(pending)
        notifier = self._bridge.ha_notifier
        if notifier is not None:
            try:
                await asyncio.to_thread(notifier.clear, package_notification_tag(device_id))
            except Exception as exc:  # noqa: BLE001 - still log the acknowledgement
                logger.warning("Clearing package notification for %s failed: %s", device_id, exc)
        self._record(device_id, "acknowledged")

    # -- log ------------------------------------------------------------------------

    def _record(
        self,
        device_id: str,
        kind: PackageEventKind,
        *,
        visitor_present: bool | None = None,
        media: list[str] | None = None,
    ) -> None:
        try:
            self._bridge.package_log.record(
                PackageEvent(
                    timestamp=datetime.now(UTC),
                    device_id=device_id,
                    kind=kind,
                    visitor_present=visitor_present,
                    media=media or [],
                )
            )
        except Exception as exc:  # noqa: BLE001 - logging never blocks an alert
            logger.warning("Package log write failed for %s: %s", device_id, exc)
