"""Telling the household: push notifications (Home Assistant) and phone
calls (SIP) for what happens at a door.

Everything here is about *who gets told what* -- the per-intent wording,
the action buttons, the attached photo, the SIP announcement and its
cooldown. None of it is specific to UniFi: a connector supplies a way to
fetch a snapshot and this class does the rest, so any bridge (UniFi, Frigate,
Home Assistant) can notify the household the same way.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from knock.core.doors import Door
from knock.core.events import VisitorEvent
from knock.core.outbound_text import for_call, for_push, one_line
from knock.core.responses import ResponseDecision
from knock.core.snapshot_store import SnapshotStore
from knock.integrations.homeassistant import (
    ACTION_DEVICE_ID_SEP,
    KNOCK_COMING_TO_DOOR_ACTION,
    KNOCK_ON_MY_WAY_ACTION,
    KNOCK_SET_DOWN_ACTION,
    KNOCK_TURN_AWAY_ACTION,
    HomeAssistantNotifier,
    NotificationCategory,
)
from knock.integrations.sip_caller import SipCaller, to_sip_audio
from knock.providers.tts.base import TTSProvider

logger = logging.getLogger(__name__)

_EMERGENCY_QUOTE_CHARS = 120


def location_text(device_id: str, door: Door | None) -> str:
    """How a notification says where a visit is. Implicit doors (and a bare
    camera ID, as direct callers pass) keep today's exact `(camera: <id>)`;
    only an explicit door reads as `(door: <name>)`.
    """
    if door is None or door.implicit:
        return f"(camera: {device_id})"
    return f"(door: {door.name})"


class HouseholdNotifier:
    def __init__(
        self,
        *,
        fetch_snapshot: Callable[[str, bool], Awaitable[bytes | None]],
        extract_details: Callable[..., Any],
        ha_notifier: HomeAssistantNotifier | None = None,
        sip_caller: SipCaller | None = None,
        tts_provider: TTSProvider | None = None,
        snapshot_store: SnapshotStore | None = None,
        public_base_url: str | None = None,
    ) -> None:
        # How to get a camera's snapshot, and the orchestrator's notification
        # wording helper -- both late-bound by the connector so swapping the
        # client or orchestrator after construction keeps working.
        self._fetch_snapshot = fetch_snapshot
        self._extract_details = extract_details
        # Optional: lets the household get an actual phone notification for
        # situations that need a human, not just a spoken reply at the door.
        # None by default (not every deployment also runs Home Assistant);
        # notify() itself is already a no-op without a configured
        # notify_service, so this stays harmless either way.
        self.ha_notifier = ha_notifier
        self.snapshot_store = snapshot_store or SnapshotStore()
        # None (the default) means notifications go out without a photo
        # attachment -- see _build_snapshot_url. Needs to be an externally-
        # reachable URL since Home Assistant's mobile app fetches it
        # directly from the household's phone, not through KNOCK's own
        # session system.
        self.public_base_url = public_base_url
        # Optional phone calls for important notifications -- see
        # `_maybe_call_household`.
        self.sip_caller = sip_caller
        self.tts_provider = tts_provider
        self._last_call_at: dict[str, float] = {}
        self._call_tasks: set[asyncio.Task[None]] = set()

    def _approval_actions(self, device_id: str, *, set_down: bool = False) -> list[dict[str, str]]:
        """The "I'm on my way" / "Turn them away" button pair for a
        notification that needs a household decision, not just an FYI.

        The device id rides along embedded in the action identifier itself
        (parsed back out by `HomeAssistantActionListener`) -- the simplest
        way to know which camera a button tap refers to, without depending
        on whatever extra context a given Home Assistant mobile app version
        does or doesn't echo back on the action event.
        """
        on_my_way = f"{KNOCK_ON_MY_WAY_ACTION}{ACTION_DEVICE_ID_SEP}{device_id}"
        turn_away = f"{KNOCK_TURN_AWAY_ACTION}{ACTION_DEVICE_ID_SEP}{device_id}"
        actions = [{"action": on_my_way, "title": "I'm on my way"}]
        if set_down:
            # Food can't sit on a porch like a package, but the household
            # may still be fine with the driver setting it somewhere safe.
            set_down_id = f"{KNOCK_SET_DOWN_ACTION}{ACTION_DEVICE_ID_SEP}{device_id}"
            actions.append({"action": set_down_id, "title": "Find a place to set it"})
        actions.append({"action": turn_away, "title": "Turn them away"})
        return actions

    def _coming_to_door_action(self, device_id: str) -> list[dict[str, str]]:
        """A single acknowledgement button for a service_appointment
        notification -- unlike `_approval_actions`, there's no "turn them
        away" decision here (the technician is already expected), just a
        way to tell a waiting visitor someone's coming.
        """
        coming = f"{KNOCK_COMING_TO_DOOR_ACTION}{ACTION_DEVICE_ID_SEP}{device_id}"
        return [{"action": coming, "title": "I'm coming to the door"}]

    async def _notify_household(
        self,
        device_id: str,
        message: str,
        *,
        actions: list[dict[str, str]] | None = None,
        category: NotificationCategory = "fyi",
        image_url: str | None = None,
        tag: str | None = None,
        door: Door | None = None,
        call_message: str | None = None,
    ) -> None:
        """Best-effort, off the event loop (the HTTP call itself is
        synchronous) -- a down or unconfigured Home Assistant never affects
        the rest of the response. Also places a phone call for the
        categories `SipConfig.call_on` lists (see `_maybe_call_household`).

        Text that may carry someone else's words is made safe per channel
        (see core/outbound_text.py): the push is one bounded line, flagged if
        it holds a link or number; the call never reads one aloud.
        `call_message` is what the call says when it should differ from the
        push (an emergency call doesn't repeat what the visitor said).
        """
        self._maybe_call_household(
            device_id,
            for_call(call_message if call_message is not None else message),
            category,
            door,
        )
        message = for_push(message)
        if self.ha_notifier is None:
            return
        # Only passed when set, so the default per-category tag stays the
        # notifier's own decision.
        extra: dict[str, Any] = {"tag": tag} if tag is not None else {}
        try:
            await asyncio.to_thread(
                self.ha_notifier.notify,
                message,
                actions=actions,
                category=category,
                image=image_url,
                **extra,
            )
        except Exception as exc:  # noqa: BLE001 - notification is best-effort
            logger.warning("Home Assistant notify failed for device %s: %s", device_id, exc)

    def _maybe_call_household(
        self,
        device_id: str,
        message: str,
        category: NotificationCategory,
        door: Door | None = None,
    ) -> None:
        """Ring the house phones for this notification, in the background.

        Never blocks the visit (a call can ring for 30s+), never replaces
        the push notification, and at most one call per category per
        `call_cooldown_seconds` -- a visitor can trip an emergency
        escalation, so they mustn't be able to make the phones ring on a
        loop. Needs TTS to have anything to say.
        """
        caller = self.sip_caller
        if caller is None or not caller.configured or category not in caller.config.call_on:
            return
        if self.tts_provider is None:
            logger.info("SIP call for %s skipped: no TTS provider to speak it", category)
            return
        now = time.monotonic()
        last = self._last_call_at.get(category)
        if last is not None and now - last < caller.config.call_cooldown_seconds:
            logger.info("SIP call for %s skipped: within the call cooldown", category)
            return
        self._last_call_at[category] = now
        task = asyncio.get_running_loop().create_task(
            self._call_household(
                caller,
                self.tts_provider,
                device_id,
                message,
                door.call_targets if door is not None else (),
            )
        )
        self._call_tasks.add(task)
        task.add_done_callback(self._call_tasks.discard)

    async def _call_household(
        self,
        caller: SipCaller,
        tts: TTSProvider,
        device_id: str,
        message: str,
        targets: Sequence[str] = (),
    ) -> None:
        try:
            spoken = await tts.synthesize(f"This is KNOCK, about the front door. {message}")
            audio = await asyncio.to_thread(to_sip_audio, spoken)
            # A door with its own call targets rings those instead of the
            # global SIP list; everything else announces exactly as before.
            if targets:
                outcome = await asyncio.to_thread(
                    functools.partial(caller.announce, audio, targets=list(targets))
                )
            else:
                outcome = await asyncio.to_thread(caller.announce, audio)
        except Exception as exc:  # noqa: BLE001 - a call is best-effort
            logger.warning("SIP call for device %s failed: %s", device_id, exc)
            return
        if outcome.answered_by:
            logger.info("SIP call for %s answered by %s", device_id, outcome.answered_by)
        else:
            logger.info(
                "SIP call for %s not answered (tried %s)%s",
                device_id,
                ", ".join(outcome.attempted) or "nobody",
                f": {outcome.error}" if outcome.error else "",
            )

    async def _build_snapshot_url(self, device_id: str, *, package: bool = False) -> str | None:
        """Best-effort: None (no photo attached, notification still sent)
        if `public_base_url` isn't configured, the snapshot fetch fails, or
        the camera has no snapshot available. `package` selects the
        doorbell's package camera (a separate, downward-angled lens) over
        the main view -- a far more useful photo for a delivery than seeing
        the front of the house.
        """
        if not self.public_base_url:
            return None
        try:
            snapshot = await self._fetch_snapshot(device_id, package)
        except Exception as exc:  # noqa: BLE001 - photo attachment is best-effort
            logger.warning("Snapshot fetch failed for device %s: %s", device_id, exc)
            return None
        if snapshot is None:
            return None
        return self._snapshot_url_for(snapshot)

    def _snapshot_url_for(self, snapshot: bytes) -> str | None:
        """A notification-attachable link for already-captured image bytes
        (see `_build_snapshot_url`), or None without `public_base_url`.
        """
        if not self.public_base_url:
            return None
        token = self.snapshot_store.put(snapshot)
        return f"{self.public_base_url.rstrip('/')}/api/snapshot/{token}"

    async def notify_nobody_visible(self, device_id: str) -> None:
        """The doorbell rang but nobody was in view (a ring-and-ditch): KNOCK
        stays silent, and the household gets a quiet FYI with the photo."""
        image_url = await self._build_snapshot_url(device_id)
        await self._notify_household(
            device_id,
            "The doorbell rang, but nobody is visible at the door.",
            category="fyi",
            image_url=image_url,
        )

    async def _on_escalate_notify(
        self,
        decision: ResponseDecision,
        visitor_event: VisitorEvent,
        device_id: str,
        door: Door | None = None,
    ) -> None:
        """An emergency alert goes out immediately -- ahead of speaking,
        since TTS synthesis/streaming takes real seconds an urgent
        notification shouldn't wait on.
        """
        if decision.escalate:
            # The visitor's words are only quoted in the push (bounded). The
            # phone call says there is a possible emergency, nothing more:
            # whoever is shouting at the door doesn't get to script it.
            quote = one_line(visitor_event.text, _EMERGENCY_QUOTE_CHARS).replace('"', "'")
            await self._notify_household(
                device_id,
                f'Possible emergency at the door: "{quote}"',
                category="emergency",
                door=door,
                call_message="Possible emergency at the door.",
            )

    async def _handle_turn_notifications(
        self,
        decision: ResponseDecision,
        visitor_event: VisitorEvent,
        device_id: str,
        door: Door | None = None,
    ) -> None:
        """Per-intent household notification, called after speaking so it
        reflects what was just said out loud (a signature-required delivery
        gets the "give me a sec" acknowledgement spoken first, *then* the
        actual notification goes out).
        """
        intent = decision.intent or "unknown"
        where = location_text(device_id, door)
        # Notification buttons talk back through the door's speaker.
        speak_device = door.speak_device if door is not None else device_id

        if decision.needs_review:
            # Closes the gap where a visitor's phrasing matched neither
            # the keyword rules nor a confident LLM category: previously
            # this silently fell through as "unknown" with zero
            # notification (see Orchestrator._refine_unknown_intent_via_tools
            # for how "uncertain" is decided). Mutually exclusive with
            # the intent-specific branches below -- this is already the
            # richer notification (summary, top-3 guesses, photo), so
            # sending both would double-notify the household for one visit.
            candidate_text = (
                ", ".join(decision.review_candidates) if decision.review_candidates else "unclear"
            )
            fallback = f"Unsure what a visitor at the door wants {where}."
            details = self._extract_details(
                visitor_event.text, intent, fallback=fallback, scene=visitor_event.scene
            )
            summary = decision.review_summary or details.summary
            # Main camera, not the package camera -- the uncertain case
            # is "who/what is this," not a known delivery.
            image_url = await self._build_snapshot_url(device_id, package=False)
            await self._notify_household(
                device_id,
                f"{summary} (best guesses: {candidate_text})",
                category="review",
                image_url=image_url,
                door=door,
            )

        elif intent in ("delivery_signature_required", "food_delivery"):
            # Both are time-sensitive in a way a plain delivery isn't
            # (can't just be left indefinitely), so the household gets
            # "I'm on my way" / "Turn them away" buttons, not just an
            # FYI -- see `_approval_actions` for why the device id rides
            # along embedded in the action identifier itself.
            fallback = (
                f"A delivery at the door needs a signature {where}."
                if intent == "delivery_signature_required"
                else f"A food delivery is at the door {where}."
            )
            details = self._extract_details(
                visitor_event.text, intent, fallback=fallback, scene=visitor_event.scene
            )
            # The doorbell's package camera (a separate, downward-angled
            # lens from the main view) shows the actual package instead
            # of just the front of the house.
            image_url = await self._build_snapshot_url(device_id, package=True)
            # Prefer a label-led summary (carrier/recipient/tracking, read
            # off the package camera's own photo -- see
            # OllamaVisionProvider.read_package_label) over the generic,
            # speech-derived one whenever the label was legible enough to
            # at least name a carrier. No label (no vision provider, or
            # nothing legible) falls back to today's unchanged summary.
            label = (
                visitor_event.scene.observation.package_label
                if visitor_event.scene is not None and visitor_event.scene.observation is not None
                else None
            )
            summary = details.summary
            if label is not None and label.carrier:
                parts = [label.carrier, "package"]
                if label.recipient_name:
                    parts.append(f"for {label.recipient_name}")
                if label.tracking_number:
                    parts.append(f"(tracking ...{label.tracking_number[-4:]})")
                summary = " ".join(parts)
            await self._notify_household(
                device_id,
                summary,
                actions=self._approval_actions(speak_device, set_down=intent == "food_delivery"),
                category="approval",
                image_url=image_url,
                door=door,
            )

        elif intent in ("official_visit", "suspicious_activity", "ride_arrived"):
            # No button-press decision to make here, just an FYI.
            # ride_arrived is here because a waiting driver is
            # time-sensitive the same way food is; visitation
            # deliberately isn't -- a routine friendly visit doesn't
            # need to interrupt anyone with a push notification.
            fallback = {
                "official_visit": "Someone claiming official business is at the door",
                "suspicious_activity": "Possibly concerning activity at the door",
                "ride_arrived": "A rideshare/taxi driver is here for pickup",
            }[intent] + f" {where}."
            details = self._extract_details(
                visitor_event.text, intent, fallback=fallback, scene=visitor_event.scene
            )
            image_url = await self._build_snapshot_url(device_id, package=False)
            await self._notify_household(device_id, details.summary, image_url=image_url, door=door)

        elif intent == "service_appointment":
            # A technician who's arrived and getting no response will
            # leave, so this carries a single acknowledgement button
            # (see `_coming_to_door_action`) rather than a plain FYI --
            # there's no "turn them away" decision since the
            # appointment is already expected, just a way to tell a
            # waiting visitor someone's coming.
            fallback = f"A technician has arrived for a service appointment {where}."
            details = self._extract_details(
                visitor_event.text, intent, fallback=fallback, scene=visitor_event.scene
            )
            image_url = await self._build_snapshot_url(device_id, package=False)
            await self._notify_household(
                device_id,
                details.summary,
                actions=self._coming_to_door_action(speak_device),
                category="approval",
                image_url=image_url,
                door=door,
            )
