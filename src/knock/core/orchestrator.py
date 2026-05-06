from datetime import datetime

from knock.conversation.intent import classify_intent
from knock.conversation.policy import PolicyEngine
from knock.conversation.responses import response_for
from knock.core.events import VisitorEvent
from knock.core.responses import ResponseDecision
from knock.core.state import SessionState


class Orchestrator:
    def __init__(self, policy: PolicyEngine | None = None) -> None:
        self.policy = policy or PolicyEngine()

    def respond(self, event: VisitorEvent, state: SessionState | None = None) -> ResponseDecision:
        allowed, reason, flags = self.policy.evaluate(event.text)

        if reason == "emergency":
            return ResponseDecision(
                text=response_for("emergency"), safe=True, escalate=True, reason="emergency"
            )

        if not allowed:
            return ResponseDecision(
                text=response_for("blocked_request"), safe=True, escalate=False, reason=reason
            )

        intent = classify_intent(event.text)
        response = self.policy.apply_style(response_for(intent))

        if state is not None:
            state.turn_count += 1
            state.last_intent = intent
            state.updated_at = datetime.now(tz=event.timestamp.tzinfo)
            state.history.append(event.text)

        return ResponseDecision(text=response, safe=True, escalate=False, reason="normal")
