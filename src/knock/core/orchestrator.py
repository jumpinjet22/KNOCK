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
    "delivery": (
        "a package delivery -- the policy is to accept it: say it's fine to "
        "leave the package by the door. Don't refuse it, don't say "
        "deliveries aren't expected or accepted, and don't redirect them to "
        "anyone else"
    ),
    "delivery_signature_required": (
        "a package delivery that requires a signature -- say you'll let the "
        "homeowner know they need to come sign for it; don't say the "
        "delivery can't be accepted or turn the driver away"
    ),
    "food_delivery": (
        "a food delivery (pizza, takeout, DoorDash/Grubhub/Uber Eats, etc). "
        "Say something like \"Thanks, I'll let them know right away so "
        "they can come grab it from you\" -- unlike a package, food can't "
        "just be left at the door. Keep it that simple and direct; don't "
        "add conditions like \"when they're ready,\" don't say to leave it "
        "by the door, don't say deliveries aren't accepted or expected, "
        "and don't mention letting the driver in or opening the door for "
        "any reason -- the homeowner comes out to the driver, never the "
        "other way around"
    ),
    "religious_soliciting": (
        "someone doing religious canvassing or solicitation -- say "
        'something like "Thanks, but we are not interested in religious '
        'materials today." Keep it brief and firm; never imply anyone is '
        "home, away, inside, or available later, and never invite them to "
        "come back or speak to someone else"
    ),
    "political_soliciting": (
        "someone doing political canvassing or collecting signatures/votes "
        '-- say something like "Thanks, but we do not discuss politics or '
        'take campaign materials at the door." Keep it brief and firm; '
        "never imply anyone is home, away, inside, or available later"
    ),
    "soliciting": (
        "a door-to-door salesperson or solicitor -- say something like "
        '"Sorry, we do not accept solicitations here. Please do not leave '
        'anything." Keep it brief and firm; never imply anyone is home, '
        "away, inside, or available later"
    ),
    "ride_arrived": (
        "a rideshare or taxi driver announcing they've arrived to pick "
        "someone up -- say something like \"Thanks, I'll let them know "
        'their ride is here." Keep it brief; never confirm whether the '
        "person is currently home or ready, just that you'll pass the "
        "message along"
    ),
    "visitation": (
        "a personal or social visit -- a friend, family member, or "
        "neighbor stopping by (not a stranger asking for someone by name, "
        'not a delivery or solicitor) -- say something like "Thanks, '
        "I'll let them know you're here!\" Keep it warm, not transactional; "
        "never confirm whether anyone is currently home or available"
    ),
    "service_appointment": (
        "a contractor or technician arriving for a scheduled service "
        "appointment. Say something like \"Thanks, I'll let them know "
        "you're here for the appointment.\" The visitor IS the expected "
        "technician -- don't imply someone else (a separate tech, a "
        "representative) is coming to meet them or is still on the way; "
        "just acknowledge them and say you'll let the homeowner know "
        "they've arrived. Don't confirm whether anyone is currently home"
    ),
    "person_lookup": (
        "someone asking for a specific person by name, not asking in "
        "general whether anyone is home -- say you'll pass along that "
        "they're looking for them; never confirm whether that person is "
        "currently home"
    ),
    "official_visit": (
        "someone claiming to be police, a government official, or a "
        "utility/service worker on official business -- don't confirm or "
        "deny anyone is home, don't grant entry or any access, just say "
        "the household will be made aware"
    ),
    "suspicious_activity": (
        "behavior or language that's concerning but doesn't rise to an "
        "emergency (e.g. lingering, casing the property, vague threats) "
        "-- respond briefly and non-confrontationally; never confirm "
        "whether anyone is home"
    ),
    "unknown": (
        "something that didn't match any of the system's known categories "
        "-- say something like \"Sorry, I didn't quite catch that, could "
        'you try again?" Keep it brief and neutral; never imply anyone is '
        "home, away, inside, or available later, and never mention a "
        "resident returning or anyone's schedule"
    ),
}

# When `classify_intent()`'s keyword rules find nothing (`"unknown"`), the
# LLM gets a chance to recognize it as any real intent before KNOCK gives up
# -- see `_refine_unknown_intent`. This covers every intent, not just the
# ones with no keyword list at all: natural language has endless phrasing a
# fixed keyword list can never fully anticipate (e.g. "I'm here to deliver
# food" matched no keyword before this existed, even though food_delivery
# obviously fits), so the LLM is a genuine safety net for keyword misses,
# not just a way to recognize wholly new categories.
#
# Deliberately a closed list parsed exactly, not free-form text: this only
# ever runs *after* PolicyEngine has already allowed the message through,
# so a wrong or unparseable guess here just leaves it at "unknown" (today's
# existing behavior), never anywhere near the emergency/blocked path.
_LLM_CLASSIFIABLE_INTENTS = [
    "delivery",
    "delivery_signature_required",
    "food_delivery",
    "religious_soliciting",
    "political_soliciting",
    "soliciting",
    "ride_arrived",
    "visitation",
    "service_appointment",
    "person_lookup",
    "official_visit",
    "suspicious_activity",
]

# Short glosses shown alongside each label in `_classification_prompt` --
# a bare category name (e.g. "visitation") alone isn't always enough for
# the model to confidently recognize when it applies, especially next to
# an instruction that deliberately biases toward "unknown." These are
# intentionally terse; `_INTENT_DESCRIPTIONS` above carries the full
# phrasing instructions for the separate _response_prompt step.
_INTENT_CLASSIFICATION_HINTS = {
    "delivery": "a package delivery",
    "delivery_signature_required": "a delivery that needs a signature",
    "food_delivery": "a food delivery (pizza, takeout, etc)",
    "religious_soliciting": "religious canvassing",
    "political_soliciting": "political canvassing",
    "soliciting": "a door-to-door salesperson",
    "ride_arrived": "a rideshare/taxi driver arriving for pickup",
    "visitation": "a casual personal visit from a friend or family member",
    "service_appointment": "a contractor/technician for a scheduled appointment",
    "person_lookup": "someone asking for a specific person by name",
    "official_visit": "police, a government official, or a utility worker",
    "suspicious_activity": "concerning but non-emergency behavior",
}


def _classification_prompt(visitor_text: str) -> str:
    labels = (
        ", ".join(
            f"{name} ({_INTENT_CLASSIFICATION_HINTS[name]})" for name in _LLM_CLASSIFIABLE_INTENTS
        )
        + ", unknown"
    )
    return (
        f'A visitor at the door said or triggered: "{visitor_text}"\n'
        "The system's keyword rules found no match. Decide whether this "
        f"clearly and specifically fits one of these categories: {labels}.\n"
        "Only choose a specific category if it obviously and unambiguously "
        'applies. "unknown" is the correct answer most of the time -- for '
        "small talk, vague chatter, irrelevant questions, or anything that "
        'does not clearly match, reply exactly "unknown". When in doubt, '
        'reply "unknown".\n'
        "Reply with only the matching category's name (one word, lowercase), nothing else."
    )


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
            if intent == "unknown":
                intent = self._refine_unknown_intent(event.text)
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
                response_text=response.text,
                session_id=state.session_id if state is not None else None,
                matched_flags=decision.confidence,
                matched_rule_ids=decision.matched_rule_ids,
                allowed=decision.allowed,
                reason=response.reason,
                intent=intent,
            )
        )

        logger.info(
            "Orchestrator decision: text=%r intent=%r reason=%r escalate=%s response=%r",
            event.text,
            last_intent,
            response.reason,
            response.escalate,
            response.text,
        )

        return response

    def _refine_unknown_intent(self, visitor_text: str) -> str:
        """One more chance to recognize a known-but-unlisted situation (a
        contractor, someone asking for a person by name, an official
        visit, concerning-but-not-emergency behavior) before `classify_intent`
        truly gives up on a message.

        Same best-effort pattern as `_text_for_intent`: no provider, a
        failure, or a reply outside `_LLM_CLASSIFIABLE_INTENTS` all fall
        back to "unknown" -- today's existing behavior, never a regression.
        """
        if self.llm_provider is None:
            return "unknown"

        try:
            guess = self.llm_provider.generate(_classification_prompt(visitor_text)).strip().lower()
        except Exception as exc:  # noqa: BLE001 - best-effort, falls back below
            logger.warning("LLM intent classification failed: %s", exc)
            return "unknown"

        result = guess if guess in _LLM_CLASSIFIABLE_INTENTS else "unknown"
        logger.info("LLM intent refinement: text=%r guess=%r -> %s", visitor_text, guess, result)
        return result

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
