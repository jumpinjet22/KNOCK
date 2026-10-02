import logging
from datetime import datetime

from knock.conversation.intent import classify_intent
from knock.conversation.policy import PolicyEngine
from knock.conversation.prompts import GREETING
from knock.conversation.responses import response_for
from knock.core.audit import AuditEntry, AuditLog, NullAuditLog
from knock.core.events import VisitorEvent
from knock.core.responses import ResponseDecision
from knock.core.state import SessionState
from knock.providers.llm.base import LLMProvider

logger = logging.getLogger(__name__)


def _with_greeting(text: str, *, is_first_turn: bool) -> str:
    return f"{GREETING} {text}" if is_first_turn else text


def _unknown_intent_prompt(text: str) -> str:
    return (
        f'A visitor at the door said: "{text}"\n'
        "This didn't match any of the system's known request categories "
        "(package delivery, emergency, asking whether anyone is home). "
        "Reply with one short, polite sentence a doorbell assistant could "
        "say back. Never say whether anyone is home, share the household's "
        "schedule, or offer to unlock/open the door."
    )


class Orchestrator:
    def __init__(
        self,
        policy: PolicyEngine | None = None,
        audit_log: AuditLog | None = None,
        llm_provider: LLMProvider | None = None,
    ) -> None:
        self.policy = policy or PolicyEngine()
        self.audit_log = audit_log or NullAuditLog()
        # Optional: visitor speech the keyword classifier can't place at all
        # falls back to this (see `_unknown_response`) instead of the static
        # "Sorry, I can't help with that right now." -- every known, safety-
        # relevant category (delivery, emergency, occupancy/schedule/unlock
        # probes) is still handled deterministically by `classify_intent`/
        # `PolicyEngine` before this is ever consulted, so a down or slow
        # LLM only ever degrades the *unknown* bucket, never the safety path.
        self.llm_provider = llm_provider

    def respond(
        self,
        event: VisitorEvent,
        state: SessionState | None = None,
        audit_log: AuditLog | None = None,
        *,
        suppress_greeting: bool = False,
    ) -> ResponseDecision:
        decision = self.policy.evaluate(event.text)
        intent: str | None = None
        # Read before any state mutation below -- true only for a session's
        # very first call. No state at all (stateless/library use) never
        # greets, matching prior behavior exactly. `suppress_greeting` is for
        # a caller (e.g. UnifiBridge) that already spoke the greeting out
        # loud itself before this ever ran, so it isn't said twice.
        is_first_turn = state is not None and state.turn_count == 0 and not suppress_greeting

        if decision.reason == "emergency":
            # No greeting here on purpose -- an emergency escalation should
            # be immediate, not prefaced with a self-introduction.
            last_intent = "emergency"
            response = ResponseDecision(
                text=response_for("emergency"), safe=True, escalate=True, reason="emergency"
            )
        elif not decision.allowed:
            last_intent = decision.reason
            response = ResponseDecision(
                text=_with_greeting(response_for(decision.reason), is_first_turn=is_first_turn),
                safe=True,
                escalate=False,
                reason=decision.reason,
            )
        else:
            intent = classify_intent(event.text)
            last_intent = intent
            response_text = self.policy.apply_style(self._text_for_intent(intent, event.text))
            response_text = _with_greeting(response_text, is_first_turn=is_first_turn)
            response = ResponseDecision(
                text=response_text, safe=True, escalate=False, reason="normal"
            )

        # Every turn advances session state the same way, regardless of
        # whether it was answered, blocked, or escalated -- previously only
        # the "normal" branch did this, so a run of blocked/emergency turns
        # never left turn 0, which (among other things) made the one-time
        # greeting above repeat on every blocked attempt instead of just the
        # session's first.
        if state is not None:
            state.turn_count += 1
            state.last_intent = last_intent
            state.updated_at = datetime.now(tz=event.timestamp.tzinfo)
            state.history.append(event.text)

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

    def _text_for_intent(self, intent: str, visitor_text: str) -> str:
        if intent != "unknown" or self.llm_provider is None:
            return response_for(intent)

        try:
            generated = self.llm_provider.generate(_unknown_intent_prompt(visitor_text)).strip()
        except Exception as exc:  # noqa: BLE001 - best-effort, falls back below
            logger.warning("LLM fallback for an unclassified request failed: %s", exc)
            return response_for(intent)

        return generated or response_for(intent)
