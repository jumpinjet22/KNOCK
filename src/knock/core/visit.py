"""Running one visit at a door: resume or start the session, greet, listen,
answer, repeat -- with every camera-facing action going through a host.

Nothing here is specific to UniFi. A connector (UniFi today; Frigate or Home
Assistant could be others) implements `VisitHost`: it knows how to listen and
speak on a device, take a snapshot, and hand over the shared services
(orchestrator, session store, audit log, providers). `VisitRunner` supplies
the rest -- the same visit behavior no matter which system saw the visitor.

The host is read on every call rather than copied at construction, so a
service replaced after the host is built (a different orchestrator, a
fake vision provider in a test) is the one the visit uses.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from knock.core.audit import AuditLog
from knock.core.conversation_runner import ConversationHooks, run_conversation_loop
from knock.core.doors import Door, DoorRegistry
from knock.core.events import VisitorEvent
from knock.core.orchestrator import Orchestrator
from knock.core.responses import ResponseDecision
from knock.core.scene import SceneContext, SceneObservation
from knock.core.session_store import SessionStore
from knock.core.state import SessionState
from knock.core.systems_check import (
    is_systems_check_trigger,
    run_systems_check,
    summarize_systems_check,
)
from knock.providers.stt.base import STTProvider
from knock.providers.tts.base import TTSProvider
from knock.providers.vision.base import VisionProvider, observe_scene, read_package_label

logger = logging.getLogger(__name__)

# A per-camera session has no expiry on its own -- without this, a greeting
# meant to play once per *visit* would instead play exactly once ever for a
# given camera, never again for any later, unrelated visitor. Two distinct
# triggers for a fresh visit: an explicit `ring` (a visitor pressing the
# button is as clear a "this is a new visit" signal as exists -- always
# restarts), or enough idle time since the session's last turn that whatever
# was happening before is almost certainly over.
_SESSION_IDLE_TIMEOUT = timedelta(minutes=5)

# Safety cap on how many listen/respond round-trips one visit can run,
# regardless of how talkative the visitor is -- end-of-conversation
# detection (see `handle_event`) is "the visitor went quiet," which is a
# good default but not a guarantee against e.g. background noise keeping
# Whisper returning short junk transcripts forever.
_MAX_CONVERSATION_TURNS = 5


def _merge_observations(main: SceneObservation, extra: SceneObservation) -> SceneObservation:
    """Fold the package camera's view into the main camera's observation:
    the package lens is what actually sees a box on the porch, the main
    lens is what sees the person, so each contributes what it's good at.
    """
    carrying = list(dict.fromkeys([*main.carrying, *extra.carrying]))
    package_visible = (
        True
        if main.package_visible or extra.package_visible
        else (main.package_visible if main.package_visible is not None else extra.package_visible)
    )
    return main.model_copy(
        update={
            "carrying": carrying,
            "package_visible": package_visible,
            "package_label": extra.package_label,
        }
    )


class VisitEvent(Protocol):
    """What a trigger looks like to a visit: an ID, a kind ("ring", a smart
    detect, "knock"), the device it came from and any detected types.
    """

    @property
    def id(self) -> str: ...

    @property
    def type(self) -> Any: ...

    @property
    def device_id(self) -> str: ...

    @property
    def smart_detect_types(self) -> Sequence[Any]: ...


class VisitTiming(Protocol):
    @property
    def ring_chime_delay_seconds(self) -> float: ...

    @property
    def ring_cooldown_seconds(self) -> float: ...


class Notifier(Protocol):
    def notify(self, message: str, **kwargs: Any) -> Any: ...


class VisitHost(Protocol):
    """Everything a visit needs from the system that saw the visitor."""

    config: Any  # a VisitTiming
    doors: DoorRegistry
    orchestrator: Orchestrator
    session_store: SessionStore
    audit_log: AuditLog
    vision_provider: VisionProvider | None
    stt_provider: STTProvider | None
    tts_provider: TTSProvider | None
    ha_notifier: Any  # a Notifier | None

    @property
    def scene_wait_seconds(self) -> float:
        """How long a reply waits on the parallel scene capture."""
        ...

    def build_event(self, event: Any) -> VisitorEvent: ...

    async def listen_to_visitor(self, device_id: str) -> str: ...

    async def speak_to_visitor(self, device_id: str, text: str) -> None: ...

    async def _play_thinking_tone(self, device_id: str) -> None: ...

    async def snapshot(self, device_id: str, *, package: bool = False) -> bytes | None: ...

    async def _on_escalate_notify(
        self,
        decision: ResponseDecision,
        visitor_event: VisitorEvent,
        device_id: str,
        door: Door | None = None,
    ) -> None: ...

    async def _handle_turn_notifications(
        self,
        decision: ResponseDecision,
        visitor_event: VisitorEvent,
        device_id: str,
        door: Door | None = None,
    ) -> None: ...


def merge_observations(main: SceneObservation, extra: SceneObservation) -> SceneObservation:
    """Fold the package camera's view into the main camera's observation:
    the package lens is what actually sees a box on the porch, the main
    lens is what sees the person, so each contributes what it's good at.
    """
    carrying = list(dict.fromkeys([*main.carrying, *extra.carrying]))
    package_visible = (
        True
        if main.package_visible or extra.package_visible
        else (main.package_visible if main.package_visible is not None else extra.package_visible)
    )
    return main.model_copy(
        update={
            "carrying": carrying,
            "package_visible": package_visible,
            "package_label": extra.package_label,
        }
    )


class VisitRunner:
    def __init__(self, host: VisitHost) -> None:
        self._host = host

    def _start_or_resume_session(
        self, session_id: str, event: VisitEvent, timestamp: datetime
    ) -> SessionState:
        """A fresh `SessionState` for a new visit, or the persisted one if
        this still looks like the same ongoing visit.

        See `_SESSION_IDLE_TIMEOUT`'s docstring for why a per-camera session
        can't just persist forever, and `UnifiConfig.ring_cooldown_seconds`'
        for why a `ring` doesn't unconditionally force a restart -- an
        impatient double-press of the button would otherwise re-play the
        entire greeting mid-conversation instead of being debounced.
        """
        existing = self._host.session_store.load(session_id)
        is_ring = str(event.type) == "ring"
        is_stale = (
            existing is not None and (timestamp - existing.updated_at) > _SESSION_IDLE_TIMEOUT
        )
        is_within_cooldown = existing is not None and (
            timestamp - existing.updated_at
        ) <= timedelta(seconds=self._host.config.ring_cooldown_seconds)

        if existing is None or is_stale or (is_ring and not is_within_cooldown):
            return SessionState(session_id=session_id, updated_at=timestamp)
        return existing

    async def _run_systems_check_and_reply(self, device_id: str, door: Door | None = None) -> None:
        """Voice-triggered diagnostic (see core/systems_check.py) -- tests
        whatever's actually configured on this bridge and speaks back a
        short result. Called by handle_event before orchestrator.respond()
        is ever reached, so this never touches PolicyEngine/classification.
        """
        vision_provider = self._host.vision_provider
        ha_notifier = self._host.ha_notifier
        look_device = door.look_devices[0] if door is not None else device_id
        speak_device = door.speak_device if door is not None else device_id

        snapshot_fetcher: Callable[[], Awaitable[bytes | None]] | None = None
        if vision_provider is not None:

            async def snapshot_fetcher() -> bytes | None:
                return await self._host.snapshot(look_device)

        notify: Callable[[str], None] | None = None
        if ha_notifier is not None:

            def notify(message: str) -> None:
                ha_notifier.notify(message, category="fyi")

        results = await run_systems_check(
            llm_provider=self._host.orchestrator.llm_provider,
            vision_provider=vision_provider,
            snapshot_fetcher=snapshot_fetcher,
            notify=notify,
        )
        summary = summarize_systems_check(results)
        logger.info("Systems check for %s: %s", device_id, summary)
        if self._host.tts_provider is not None:
            await self._host.speak_to_visitor(speak_device, summary)

    def _detector_scene(self, event: VisitEvent) -> SceneContext:
        """The scene context UniFi's own detector gives for free -- smart
        detect types (person/package/vehicle) -- with no vision model
        involved at all.
        """
        return SceneContext(
            camera=event.device_id,
            detected_labels=[str(t) for t in event.smart_detect_types],
            captured_at=datetime.now(UTC),
        )

    async def _visitor_is_present(self, event: VisitEvent, look_device: str) -> bool:
        """Is anyone actually at the door? Looks through a few snapshots; only
        a clear "zero people" on every one says no. A person detection from
        the camera, no snapshot, or any vision failure all say yes, so a
        broken check never silences a real visitor.
        """
        if "person" in {str(t) for t in getattr(event, "smart_detect_types", ()) or ()}:
            return True
        config = self._host.config
        attempts = max(1, int(config.visitor_check_attempts))
        for attempt in range(attempts):
            if attempt:
                await asyncio.sleep(config.visitor_check_interval_seconds)
            try:
                snapshot = await self._host.snapshot(look_device)
                if snapshot is None or self._host.vision_provider is None:
                    return True
                observation = await asyncio.to_thread(
                    observe_scene, self._host.vision_provider, snapshot
                )
            except Exception as exc:  # noqa: BLE001 - never silence a visitor on an error
                logger.warning("Visitor-present check failed for %s: %s", look_device, exc)
                return True
            count = observation.people_count if observation is not None else None
            if count is None or count > 0:
                return True
        notify = getattr(self._host, "_notify_nobody_visible", None)
        if notify is not None:
            try:
                await notify(look_device)
            except Exception as exc:  # noqa: BLE001 - the notification is a bonus
                logger.warning("Nobody-visible notification failed: %s", exc)
        return False

    async def _capture_scene(
        self, event: VisitEvent, door: Door | None = None
    ) -> tuple[SceneContext, bytes | None]:
        """Snapshot + structured vision observation for this event's camera,
        plus the raw snapshot (for the opt-in send-image-to-brain path).

        Best-effort like every other vision path here: no snapshot, or any
        snapshot/vision failure, still returns the detector-only scene.
        When UniFi's own detector already saw a package, the doorbell's
        package camera is looked through too and merged in.
        """
        scene = self._detector_scene(event)
        if self._host.vision_provider is None:
            return scene, None
        vision_provider = self._host.vision_provider
        # The door's main snapshot camera (the triggering camera, for an
        # implicit door).
        look_device = door.look_devices[0] if door is not None else event.device_id
        try:
            snapshot = await self._host.snapshot(look_device)
            if snapshot is None:
                return scene, None
            observation = await asyncio.to_thread(observe_scene, vision_provider, snapshot)
            if "package" in scene.detected_labels:
                try:
                    package_snapshot = await self._host.snapshot(look_device, package=True)
                    if package_snapshot is not None:
                        package_observation = await asyncio.to_thread(
                            observe_scene, vision_provider, package_snapshot
                        )
                        label = await asyncio.to_thread(
                            read_package_label, vision_provider, package_snapshot
                        )
                        if label is not None:
                            package_observation = package_observation.model_copy(
                                update={"package_label": label}
                            )
                        observation = merge_observations(observation, package_observation)
                except Exception as exc:  # noqa: BLE001 - package view is a bonus
                    logger.info(
                        "Package-camera observation failed for %s: %s", event.device_id, exc
                    )
            return scene.model_copy(update={"observation": observation}), snapshot
        except Exception as exc:  # noqa: BLE001 - vision enrichment is best-effort
            logger.warning("Vision enrichment failed for device %s: %s", event.device_id, exc)
            return scene, None

    def _systems_check_interceptor(
        self, device_id: str, door: Door | None = None
    ) -> Callable[[str], Awaitable[bool]]:
        async def intercept(transcript: str) -> bool:
            if not is_systems_check_trigger(transcript):
                return False
            await self._run_systems_check_and_reply(device_id, door)
            return True

        return intercept

    async def run(self, event: VisitEvent, door: Door | None = None) -> ResponseDecision | None:
        if door is None:
            door = self._host.doors.for_camera(event.device_id)
        if str(event.type) == "ring" and self._host.config.ring_chime_delay_seconds > 0:
            # Let the physical doorbell chime finish before KNOCK starts
            # talking over it or opens its listen window early enough to
            # capture the tail of the chime instead of just the visitor. A
            # silent smart-detect trigger has no chime to wait out.
            await asyncio.sleep(self._host.config.ring_chime_delay_seconds)

        visitor_event = self._host.build_event(event)
        state = self._start_or_resume_session(door.session_id, event, visitor_event.timestamp)

        # Every role comes from the door. For an implicit door that is the
        # triggering camera for all of them, as it always was.
        listen_device = door.listen_device
        speak_device = door.speak_device
        look_device = door.look_devices[0]
        hooks = ConversationHooks(
            listen=(lambda: self._host.listen_to_visitor(listen_device))
            if self._host.stt_provider
            else None,
            speak=(
                (lambda text: self._host.speak_to_visitor(speak_device, text))
                if self._host.tts_provider
                else None
            ),
            play_thinking_tone=(
                (lambda: self._host._play_thinking_tone(speak_device))
                if self._host.tts_provider
                else None
            ),
            capture_scene=lambda: self._capture_scene(event, door),
            confirm_visitor_present=(
                (lambda: self._visitor_is_present(event, look_device))
                if getattr(self._host.config, "require_visitor_present", False) is True
                and self._host.vision_provider is not None
                else None
            ),
            scene_fallback=lambda: self._detector_scene(event),
            intercept_transcript=self._systems_check_interceptor(look_device, door),
            before_speak=lambda d, ve: self._host._on_escalate_notify(d, ve, look_device, door),
            after_speak=lambda d, ve: self._host._handle_turn_notifications(
                d, ve, look_device, door
            ),
        )

        return await run_conversation_loop(
            visitor_event,
            state=state,
            orchestrator=self._host.orchestrator,
            session_store=self._host.session_store,
            hooks=hooks,
            audit_log=self._host.audit_log,
            max_turns=_MAX_CONVERSATION_TURNS,
            scene_wait_seconds=self._host.scene_wait_seconds,
        )
