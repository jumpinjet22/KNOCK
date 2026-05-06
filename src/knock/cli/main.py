from datetime import UTC, datetime

from knock.core.events import VisitorEvent
from knock.core.orchestrator import Orchestrator


def main() -> None:
    visitor_text = input("Visitor: ").strip()
    event = VisitorEvent(source="cli", text=visitor_text, timestamp=datetime.now(UTC))
    decision = Orchestrator().respond(event)
    print(f"Response: {decision.text}")
