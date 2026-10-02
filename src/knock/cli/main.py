import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from knock.core.events import VisitorEvent
from knock.core.orchestrator import Orchestrator
from knock.core.session_store import JSONFileSessionStore
from knock.core.state import SessionState

_EXIT_WORDS = {"quit", "exit"}


def main(get_input: Callable[[str], str] = input) -> None:
    session_id = uuid.uuid4().hex
    store = JSONFileSessionStore()
    state = SessionState(session_id=session_id, updated_at=datetime.now(UTC))
    orchestrator = Orchestrator()

    print(f"KNOCK CLI -- session {session_id} (blank line or 'quit' to exit)")

    while True:
        try:
            visitor_text = get_input("Visitor: ").strip()
        except EOFError:
            break

        if not visitor_text or visitor_text.lower() in _EXIT_WORDS:
            break

        event = VisitorEvent(source="cli", text=visitor_text, timestamp=datetime.now(UTC))
        decision = orchestrator.respond(event, state=state)
        store.save(state)
        print(f"Response: {decision.text}")

    print(f"Session saved: {session_id} ({state.turn_count} turn(s))")
