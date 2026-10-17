import asyncio
from datetime import UTC, datetime

from knock.conversation.prompts import GREETING
from knock.core.conversation_runner import ConversationHooks, run_conversation_loop
from knock.core.events import VisitorEvent
from knock.core.orchestrator import Orchestrator
from knock.core.responses import ResponseDecision
from knock.core.scene import SceneContext
from knock.core.state import SessionState


def _event(text: str = "hello") -> VisitorEvent:
    return VisitorEvent(source="test", text=text, timestamp=datetime.now(UTC))


def _state(turn_count: int = 0) -> SessionState:
    return SessionState(session_id="s1", turn_count=turn_count, updated_at=datetime.now(UTC))


class _FakeSessionStore:
    """Bare-minimum stand-in for the `SessionStore` Protocol -- proves the
    runner needs nothing UniFi-specific, just `load`/`save`/`list_ids`.
    """

    def __init__(self) -> None:
        self.saved: list[SessionState] = []

    def load(self, session_id: str) -> SessionState | None:
        return None

    def save(self, state: SessionState) -> None:
        self.saved.append(state.model_copy())

    def list_ids(self) -> list[str]:
        return []


class _ScriptedOrchestrator:
    """Returns pre-scripted decisions in order -- lets a test drive exact
    sequences (not_addressed, turn-cap counting) that aren't reachable
    deterministically through a real `Orchestrator()` without an LLM.
    """

    def __init__(self, decisions: list[ResponseDecision]) -> None:
        self._decisions = iter(decisions)
        self.calls: list[VisitorEvent] = []

    def respond(
        self,
        event: VisitorEvent,
        state: SessionState | None = None,
        audit_log: object = None,
        *,
        suppress_greeting: bool = False,
        image: bytes | None = None,
    ) -> ResponseDecision:
        self.calls.append(event)
        if state is not None:
            state.turn_count += 1
        return next(self._decisions)


def _run(*args, **kwargs):
    return asyncio.run(run_conversation_loop(*args, **kwargs))


def test_ends_after_one_turn_without_listen_or_speak_hooks() -> None:
    # No STT/TTS at all -- can_converse is False, matching a text-only
    # deployment's existing behavior.
    orchestrator = Orchestrator()
    decision = _run(
        _event("Hi, I have a package"),
        state=_state(),
        orchestrator=orchestrator,
        session_store=_FakeSessionStore(),
        hooks=ConversationHooks(),
        audit_log=None,
        max_turns=5,
        scene_wait_seconds=1.0,
    )
    assert decision is not None
    assert decision.intent == "delivery"


def test_greets_aloud_only_on_a_fresh_session_with_a_speak_hook() -> None:
    spoken: list[str] = []

    async def speak(text: str) -> None:
        spoken.append(text)

    _run(
        _event("Hi, I have a package"),
        state=_state(turn_count=0),
        orchestrator=Orchestrator(),
        session_store=_FakeSessionStore(),
        hooks=ConversationHooks(speak=speak),
        audit_log=None,
        max_turns=1,
        scene_wait_seconds=1.0,
    )
    assert spoken[0] == GREETING
    assert len(spoken) == 2  # greeting, then the one turn's reply


def test_no_greeting_on_a_resumed_session() -> None:
    spoken: list[str] = []

    async def speak(text: str) -> None:
        spoken.append(text)

    _run(
        _event("Hi, I have a package"),
        state=_state(turn_count=3),
        orchestrator=Orchestrator(),
        session_store=_FakeSessionStore(),
        hooks=ConversationHooks(speak=speak),
        audit_log=None,
        max_turns=1,
        scene_wait_seconds=1.0,
    )
    assert len(spoken) == 1  # just the reply, no greeting


def test_no_greeting_at_all_without_a_speak_hook() -> None:
    # Text-only deployment: Orchestrator.respond() still prepends the
    # greeting into decision.text itself (suppress_greeting=False), since
    # nothing else ever said it out loud.
    decision = _run(
        _event("Hi, I have a package"),
        state=_state(),
        orchestrator=Orchestrator(),
        session_store=_FakeSessionStore(),
        hooks=ConversationHooks(),
        audit_log=None,
        max_turns=1,
        scene_wait_seconds=1.0,
    )
    assert decision is not None
    assert decision.text.startswith(GREETING)


def test_escalate_ends_after_one_turn_and_calls_hooks_in_order() -> None:
    order: list[str] = []

    async def speak(text: str) -> None:
        order.append("speak")

    async def before_speak(decision: ResponseDecision, event: VisitorEvent) -> None:
        order.append("before_speak")

    async def after_speak(decision: ResponseDecision, event: VisitorEvent) -> None:
        order.append("after_speak")

    async def listen() -> str:
        order.append("listen")
        return "should never be reached"

    orchestrator = _ScriptedOrchestrator(
        [ResponseDecision(text="help is on the way", escalate=True, intent="emergency")]
    )
    _run(
        _event("fire emergency help"),
        # A resumed session (turn_count > 0) so the greeting doesn't add
        # its own "speak" to the order this test is asserting on.
        state=_state(turn_count=3),
        orchestrator=orchestrator,
        session_store=_FakeSessionStore(),
        hooks=ConversationHooks(
            listen=listen, speak=speak, before_speak=before_speak, after_speak=after_speak
        ),
        audit_log=None,
        max_turns=5,
        scene_wait_seconds=1.0,
    )
    # First "listen" is the pre-loop listen (captures the initial
    # transcript); escalate ends it before a second listen for a reply.
    assert order == ["listen", "before_speak", "speak", "after_speak"]


def test_not_addressed_ends_after_one_turn_even_though_it_can_converse() -> None:
    listened = 0

    async def listen() -> str:
        nonlocal listened
        listened += 1
        return "what are you smoking for? hello?" if listened == 1 else "should not be reached"

    async def speak(text: str) -> None:
        pass

    orchestrator = _ScriptedOrchestrator(
        [ResponseDecision(text="", intent="not_addressed", reason="not_addressed")]
    )
    decision = _run(
        _event(),
        state=_state(),
        orchestrator=orchestrator,
        session_store=_FakeSessionStore(),
        hooks=ConversationHooks(listen=listen, speak=speak),
        audit_log=None,
        max_turns=5,
        scene_wait_seconds=1.0,
    )
    assert decision is not None
    assert decision.intent == "not_addressed"
    assert listened == 1  # never listened for a second turn
    assert len(orchestrator.calls) == 1


def test_visitor_going_quiet_ends_the_conversation() -> None:
    transcripts = iter(["still talking", "still talking", ""])

    async def listen() -> str:
        return next(transcripts)

    async def speak(text: str) -> None:
        pass

    orchestrator = _ScriptedOrchestrator(
        [ResponseDecision(text="go on", intent="visitation") for _ in range(5)]
    )
    _run(
        _event(),
        state=_state(),
        orchestrator=orchestrator,
        session_store=_FakeSessionStore(),
        hooks=ConversationHooks(listen=listen, speak=speak),
        audit_log=None,
        max_turns=5,
        scene_wait_seconds=1.0,
    )
    # Two respond() calls, one per real turn -- the third, empty
    # transcript ends the loop before a third respond() ever happens.
    assert len(orchestrator.calls) == 2


def test_turn_cap_stops_at_max_turns_not_beyond() -> None:
    orchestrator = _ScriptedOrchestrator(
        [ResponseDecision(text="keep talking", intent="visitation") for _ in range(5)]
    )

    async def listen() -> str:
        return "still here"

    async def speak(text: str) -> None:
        pass

    _run(
        _event(),
        state=_state(),
        orchestrator=orchestrator,
        session_store=_FakeSessionStore(),
        hooks=ConversationHooks(listen=listen, speak=speak),
        audit_log=None,
        max_turns=2,
        scene_wait_seconds=1.0,
    )
    assert len(orchestrator.calls) == 2


def test_intercept_transcript_aborts_before_orchestrator_is_called() -> None:
    orchestrator = _ScriptedOrchestrator(
        [ResponseDecision(text="should never be produced", intent="visitation")]
    )

    async def listen() -> str:
        return "systems check"

    async def speak(text: str) -> None:
        pass

    async def intercept(transcript: str) -> bool:
        return transcript == "systems check"

    decision = _run(
        _event(),
        state=_state(),
        orchestrator=orchestrator,
        session_store=_FakeSessionStore(),
        hooks=ConversationHooks(listen=listen, speak=speak, intercept_transcript=intercept),
        audit_log=None,
        max_turns=5,
        scene_wait_seconds=1.0,
    )
    assert decision is None
    assert orchestrator.calls == []


def test_intercept_transcript_returning_false_continues_normally() -> None:
    async def listen() -> str:
        return "I have a package for you"

    async def speak(text: str) -> None:
        pass

    async def intercept(transcript: str) -> bool:
        return False

    decision = _run(
        _event(),
        state=_state(),
        orchestrator=Orchestrator(),
        session_store=_FakeSessionStore(),
        hooks=ConversationHooks(listen=listen, speak=speak, intercept_transcript=intercept),
        audit_log=None,
        max_turns=1,
        scene_wait_seconds=1.0,
    )
    assert decision is not None
    assert decision.intent == "delivery"


def test_scene_fallback_used_when_capture_scene_is_too_slow() -> None:
    async def slow_capture_scene() -> tuple[SceneContext, bytes | None]:
        await asyncio.sleep(10)
        return SceneContext(camera="too-slow"), None

    def fallback() -> SceneContext:
        return SceneContext(camera="fallback")

    seen_scenes: list[SceneContext | None] = []

    class _CapturingOrchestrator:
        def respond(
            self, event, state=None, audit_log=None, *, suppress_greeting=False, image=None
        ):
            seen_scenes.append(event.scene)
            return ResponseDecision(text="ok", intent="visitation")

    _run(
        _event(),
        state=_state(),
        orchestrator=_CapturingOrchestrator(),
        session_store=_FakeSessionStore(),
        hooks=ConversationHooks(capture_scene=slow_capture_scene, scene_fallback=fallback),
        audit_log=None,
        max_turns=1,
        scene_wait_seconds=0.01,
    )
    assert seen_scenes[0] is not None
    assert seen_scenes[0].camera == "fallback"


def test_fast_capture_scene_result_is_used_unmodified() -> None:
    async def fast_capture_scene() -> tuple[SceneContext, bytes | None]:
        return SceneContext(camera="front-door"), b"jpeg-bytes"

    seen_scenes: list[SceneContext | None] = []

    class _CapturingOrchestrator:
        def respond(
            self, event, state=None, audit_log=None, *, suppress_greeting=False, image=None
        ):
            seen_scenes.append(event.scene)
            return ResponseDecision(text="ok", intent="visitation")

    _run(
        _event(),
        state=_state(),
        orchestrator=_CapturingOrchestrator(),
        session_store=_FakeSessionStore(),
        hooks=ConversationHooks(capture_scene=fast_capture_scene),
        audit_log=None,
        max_turns=1,
        scene_wait_seconds=1.0,
    )
    assert seen_scenes[0] is not None
    assert seen_scenes[0].camera == "front-door"


def test_no_capture_scene_hook_leaves_scene_as_none() -> None:
    seen_scenes: list[SceneContext | None] = []

    class _CapturingOrchestrator:
        def respond(
            self, event, state=None, audit_log=None, *, suppress_greeting=False, image=None
        ):
            seen_scenes.append(event.scene)
            return ResponseDecision(text="ok", intent="visitation")

    _run(
        _event(),
        state=_state(),
        orchestrator=_CapturingOrchestrator(),
        session_store=_FakeSessionStore(),
        hooks=ConversationHooks(),
        audit_log=None,
        max_turns=1,
        scene_wait_seconds=1.0,
    )
    assert seen_scenes[0] is None


def test_session_store_saves_once_per_turn() -> None:
    store = _FakeSessionStore()

    async def listen() -> str:
        return "I have a package for you"

    async def speak(text: str) -> None:
        pass

    _run(
        _event(),
        state=_state(),
        orchestrator=Orchestrator(),
        session_store=store,
        hooks=ConversationHooks(listen=listen, speak=speak),
        audit_log=None,
        max_turns=1,
        scene_wait_seconds=1.0,
    )
    assert len(store.saved) == 1


def test_thinking_tone_played_once_per_listen_when_provided() -> None:
    tones = 0

    async def play_thinking_tone() -> None:
        nonlocal tones
        tones += 1

    async def listen() -> str:
        return ""  # go quiet immediately after the pre-loop listen

    async def speak(text: str) -> None:
        pass

    _run(
        _event(),
        state=_state(),
        orchestrator=Orchestrator(),
        session_store=_FakeSessionStore(),
        hooks=ConversationHooks(listen=listen, speak=speak, play_thinking_tone=play_thinking_tone),
        audit_log=None,
        max_turns=5,
        scene_wait_seconds=1.0,
    )
    assert tones == 1


def test_thinking_tone_never_played_when_not_provided() -> None:
    async def listen() -> str:
        return ""

    async def speak(text: str) -> None:
        pass

    # No play_thinking_tone hook -- should not raise, and there's nothing
    # to assert beyond "this completes without error."
    _run(
        _event(),
        state=_state(),
        orchestrator=Orchestrator(),
        session_store=_FakeSessionStore(),
        hooks=ConversationHooks(listen=listen, speak=speak),
        audit_log=None,
        max_turns=5,
        scene_wait_seconds=1.0,
    )
