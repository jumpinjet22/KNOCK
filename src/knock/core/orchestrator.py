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


# Plain-English framing of each `classify_intent()` bucket, embedded in the
# LLM prompt below so it understands the classifier's guess without needing
# to know KNOCK's internal intent names.
_INTENT_DESCRIPTIONS = {
    "delivery": "a package delivery",
    "delivery_signature_required": (
        "a package delivery that requires a signature -- say you'll let the "
        "homeowner know they need to come sign for it; don't say the "
        "delivery can't be accepted or turn the driver away"
    ),
    "religious_soliciting": "someone doing religious canvassing or solicitation",
    "political_soliciting": "someone doing political canvassing or collecting signatures/votes",
    "soliciting": "a door-to-door salesperson or solicitor",
    "unknown": "something that didn't match any of the system's known categories",
}


def _response_prompt(intent: str, visitor_text: str) -> str:
    category = _INTENT_DESCRIPTIONS.get(intent, intent)
    return (
        f'A visitor at the door said or triggered: "{visitor_text}"\n'
        f"The system's keyword classifier guesses this is: {category}.\n"
        "Reply with one short, natural, polite sentence a doorbell "
        "assistant could say back, consistent with that situation -- don't "
        "just recite the category. Never say whether anyone is home, share "
        "the household's schedule, or offer to unlock/open the door."
    )


def _notification_summary_prompt(visitor_text: str) -> str:
    return (
        f'Context from a doorbell visit: "{visitor_text}"\n'
        "Write one short phrase (under 12 words) summarizing who's at the "
        'door, suitable as a phone push notification -- e.g. "A FedEx '
        'driver has a package for you" or "A UPS delivery needs a '
        "signature.\" State only what's actually said/seen; don't invent "
        "a carrier or detail that isn't there. No greeting, no extra "
        "commentary, just the summary."
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
        # Optional: when set, this is the *primary* way every allowed,
        # non-emergency response gets phrased (see `_text_for_intent`) --
        # classify_intent()'s category becomes a hint in the prompt rather
        # than a literal canned string, so a response can actually use what
        # was transcribed/seen instead of reciting the same fixed sentence
        # every time. Emergency escalation and blocked requests (occupancy/
        # schedule/unlock probes) are still handled entirely by the
        # deterministic PolicyEngine before this is ever consulted -- those
        # never go through the LLM, so a down or unpredictable LLM only
        # ever degrades phrasing, never the safety path. A missing
        # provider, or any failure/blank reply from it, falls back to the
        # static RESPONSES text.
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
                text=response_for("emergency"),
                safe=True,
                escalate=True,
                reason="emergency",
                intent=last_intent,
            )
        elif not decision.allowed:
            last_intent = decision.reason
            response = ResponseDecision(
                text=_with_greeting(response_for(decision.reason), is_first_turn=is_first_turn),
                safe=True,
                escalate=False,
                reason=decision.reason,
                intent=last_intent,
            )
        else:
            intent = classify_intent(event.text)
            last_intent = intent
            response_text = self.policy.apply_style(self._text_for_intent(intent, event.text))
            response_text = _with_greeting(response_text, is_first_turn=is_first_turn)
            response = ResponseDecision(
                text=response_text, safe=True, escalate=False, reason="normal", intent=last_intent
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
        if self.llm_provider is None:
            return response_for(intent)

        try:
            generated = self.llm_provider.generate(_response_prompt(intent, visitor_text)).strip()
        except Exception as exc:  # noqa: BLE001 - best-effort, falls back below
            logger.warning("LLM response generation failed for intent %r: %s", intent, exc)
            return response_for(intent)

        return generated or response_for(intent)

    def summarize_for_notification(self, visitor_text: str, *, fallback: str) -> str:
        """A short, human-readable phrase for a push notification -- e.g.
        "A FedEx driver has a package for you" instead of a generic
        placeholder. Same best-effort pattern as `_text_for_intent`: no
        provider, a failure, or a blank reply all fall back to `fallback`.
        """
        if self.llm_provider is None:
            return fallback

        try:
            summary = self.llm_provider.generate(_notification_summary_prompt(visitor_text)).strip()
        except Exception as exc:  # noqa: BLE001 - best-effort, falls back below
            logger.warning("LLM notification summary failed: %s", exc)
            return fallback

        return summary or fallback
