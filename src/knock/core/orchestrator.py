from datetime import datetime

from knock.conversation.intent import classify_intent
from knock.conversation.policy import PolicyEngine
from knock.conversation.responses import response_for
from knock.core.audit import AuditEntry, AuditLog, NullAuditLog
from knock.core.events import VisitorEvent
from knock.core.responses import ResponseDecision
from knock.core.state import SessionState


class Orchestrator:
    def __init__(
        self,
        policy: PolicyEngine | None = None,
        audit_log: AuditLog | None = None,
    ) -> None:
        self.policy = policy or PolicyEngine()
        self.audit_log = audit_log or NullAuditLog()

    def respond(
        self,
        event: VisitorEvent,
        state: SessionState | None = None,
        audit_log: AuditLog | None = None,
    ) -> ResponseDecision:
        decision = self.policy.evaluate(event.text)
        intent: str | None = None

        if decision.reason == "emergency":
            response = ResponseDecision(
                text=response_for("emergency"), safe=True, escalate=True, reason="emergency"
            )
        elif not decision.allowed:
            response = ResponseDecision(
                text=response_for(decision.reason),
                safe=True,
                escalate=False,
                reason=decision.reason,
            )
        else:
            intent = classify_intent(event.text)
            response_text = self.policy.apply_style(response_for(intent))

            if state is not None:
                state.turn_count += 1
                state.last_intent = intent
                state.updated_at = datetime.now(tz=event.timestamp.tzinfo)
                state.history.append(event.text)

            response = ResponseDecision(
                text=response_text, safe=True, escalate=False, reason="normal"
            )

        (audit_log or self.audit_log).record(
            AuditEntry(
                timestamp=event.timestamp,
                text=event.text,
                matched_flags=decision.confidence,
                matched_rule_ids=decision.matched_rule_ids,
                allowed=decision.allowed,
                reason=response.reason,
                intent=intent,
            )
        )

        return response
