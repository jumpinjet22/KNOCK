"""Hardware-agnostic multi-turn conversation loop: greet, call
Orchestrator.respond() turn by turn, apply the same escalation/turn-cap/
not_addressed ending rules every integration should get for free, and call
back into bridge-specific hooks (listen, speak, scene capture, per-turn
notification side effects) at the exact points UnifiBridge's original,
hand-written loop called its own equivalent private methods.

Every hook is a plain zero/one/two-arg async callable a bridge builds as a
closure over its own device/session identity (e.g. device_id) -- this
module never sees or needs to know what kind of hardware or transport is
behind any of them. A bridge with no STT/TTS at all can simply leave
`listen`/`speak` as None and still get identical ending-condition behavior
(`can_converse` becomes False, so the conversation always ends after one
turn -- matching UnifiBridge's own text-only-deployment behavior today).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from knock.conversation.prompts import GREETING
from knock.core.audit import AuditLog
from knock.core.events import VisitorEvent
from knock.core.orchestrator import Orchestrator
from knock.core.responses import ResponseDecision
from knock.core.scene import SceneContext
from knock.core.session_store import SessionStore
from knock.core.state import SessionState

logger = logging.getLogger(__name__)


@dataclass
class ConversationHooks:
    """Hardware I/O + bridge-specific side effects, called at the exact
    points UnifiBridge's own loop used to call its private methods
    directly. Every field left as None is simply skipped -- a bridge opts
    into only the pieces it actually has.
    """

    listen: Callable[[], Awaitable[str]] | None = None
    """Capture + transcribe the visitor's next utterance; "" ends the
    conversation (visitor went quiet). None means no STT at all --
    `can_converse` becomes False."""

    speak: Callable[[str], Awaitable[None]] | None = None
    """Synthesize and play `text` aloud. None means no TTS -- greeting is
    never spoken aloud and decision.text is never played, but
    Orchestrator.respond() still prepends the greeting to its returned text
    (suppress_greeting=False), matching UnifiBridge's existing text-only
    behavior."""

    play_thinking_tone: Callable[[], Awaitable[None]] | None = None
    """Played right after each `listen()` call returns. Purely cosmetic;
    omit for an integration with no equivalent audio cue."""

    capture_scene: Callable[[], Coroutine[Any, Any, tuple[SceneContext, bytes | None]]] | None = (
        None
    )
    """Kicked off as a background task at the start of each turn, run in
    parallel with `listen()`. None means no scene/vision capture at all --
    visitor_event.scene stays whatever the caller set it to."""

    scene_fallback: Callable[[], SceneContext] | None = None
    """Used if `capture_scene`'s task hasn't finished within
    `scene_wait_seconds`. Ignored if `capture_scene` is None."""

    intercept_transcript: Callable[[str], Awaitable[bool]] | None = None
    """Called once, with the very first turn's transcript, before it is
    ever passed to Orchestrator.respond(). Returning True means the hook
    fully handled this transcript itself (e.g. an operator diagnostic
    command) -- the loop stops immediately and returns None without ever
    invoking the orchestrator."""

    before_speak: Callable[[ResponseDecision, VisitorEvent], Awaitable[None]] | None = None
    """Called once per turn, right after Orchestrator.respond() returns and
    session state is saved, but before `speak()` -- e.g. an immediate
    emergency notification that shouldn't wait on TTS synthesis time. Called
    unconditionally every turn; the hook itself is responsible for checking
    whatever condition it cares about (e.g. decision.escalate)."""

    after_speak: Callable[[ResponseDecision, VisitorEvent], Awaitable[None]] | None = None
    """Called once per turn, right after `speak()` -- per-intent household
    notification branching. Called unconditionally every turn; the hook
    checks decision.intent itself."""


async def _await_scene(
    task: asyncio.Task[tuple[SceneContext, bytes | None]] | None,
    *,
    timeout: float,
    fallback: Callable[[], SceneContext] | None,
    source: str,
) -> tuple[SceneContext | None, bytes | None]:
    if task is None:
        return None, None
    try:
        return await asyncio.wait_for(task, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - includes TimeoutError
        logger.warning("Scene capture for %s not ready, replying without it: %s", source, exc)
        return (fallback() if fallback is not None else None), None


async def run_conversation_loop(
    visitor_event: VisitorEvent,
    *,
    state: SessionState,
    orchestrator: Orchestrator,
    session_store: SessionStore,
    hooks: ConversationHooks,
    audit_log: AuditLog | None,
    max_turns: int,
    scene_wait_seconds: float,
) -> ResponseDecision | None:
    is_first_turn = state.turn_count == 0
    greeted_aloud = is_first_turn and hooks.speak is not None

    scene_task: asyncio.Task[tuple[SceneContext, bytes | None]] | None = (
        asyncio.create_task(hooks.capture_scene()) if hooks.capture_scene else None
    )

    if greeted_aloud:
        assert hooks.speak is not None
        await hooks.speak(GREETING)

    if hooks.listen is not None:
        transcript = await hooks.listen()
        if transcript:
            visitor_event = visitor_event.model_copy(update={"text": transcript})
            if hooks.intercept_transcript is not None and await hooks.intercept_transcript(
                transcript
            ):
                if scene_task is not None:
                    scene_task.cancel()
                return None
        if hooks.play_thinking_tone is not None:
            await hooks.play_thinking_tone()

    scene, snapshot = await _await_scene(
        scene_task,
        timeout=scene_wait_seconds,
        fallback=hooks.scene_fallback,
        source=visitor_event.source,
    )
    visitor_event = visitor_event.model_copy(update={"scene": scene})

    can_converse = hooks.listen is not None and hooks.speak is not None
    decision: ResponseDecision | None = None

    for turn_index in range(max_turns):
        decision = orchestrator.respond(
            visitor_event,
            state=state,
            audit_log=audit_log,
            suppress_greeting=greeted_aloud,
            image=snapshot,
        )
        session_store.save(state)

        if hooks.before_speak is not None:
            await hooks.before_speak(decision, visitor_event)
        if hooks.speak is not None:
            await hooks.speak(decision.text)
        if hooks.after_speak is not None:
            await hooks.after_speak(decision, visitor_event)

        intent = decision.intent or "unknown"
        is_last_possible_turn = turn_index == max_turns - 1
        not_addressed = intent == "not_addressed"
        if decision.escalate or not can_converse or is_last_possible_turn or not_addressed:
            reason = (
                "escalated"
                if decision.escalate
                else "cannot converse (no STT/TTS)"
                if not can_converse
                else "visitor not addressing the door"
                if not_addressed
                else "hit the max turn cap"
            )
            logger.info(
                "Conversation %s ending after turn %d: %s",
                state.session_id,
                turn_index + 1,
                reason,
            )
            break

        scene_task = asyncio.create_task(hooks.capture_scene()) if hooks.capture_scene else None
        assert hooks.listen is not None  # can_converse guarantees this
        reply = await hooks.listen()
        if not reply:
            if scene_task is not None:
                scene_task.cancel()
            logger.info(
                "Conversation %s ending after turn %d: visitor went quiet",
                state.session_id,
                turn_index + 1,
            )
            break
        if hooks.play_thinking_tone is not None:
            await hooks.play_thinking_tone()
        scene, snapshot = await _await_scene(
            scene_task,
            timeout=scene_wait_seconds,
            fallback=hooks.scene_fallback,
            source=visitor_event.source,
        )
        visitor_event = visitor_event.model_copy(
            update={"text": reply, "timestamp": datetime.now(UTC), "scene": scene}
        )

    return decision
