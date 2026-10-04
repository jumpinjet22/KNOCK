import logging
from datetime import datetime
from typing import Any, NamedTuple

from pydantic import BaseModel

from knock.conversation.intent import classify_intent
from knock.conversation.policy import PolicyEngine
from knock.conversation.prompts import GREETING, SYSTEM_PROMPT
from knock.conversation.responses import response_for
from knock.core.audit import AuditEntry, AuditLog, NullAuditLog
from knock.core.events import VisitorEvent
from knock.core.responses import ResponseDecision
from knock.core.state import SessionState
from knock.providers.llm.base import LLMProvider
from knock.providers.llm.ollama import ChatToolResult

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
        "a package delivery that requires a signature -- say you'll pass "
        "that along for a signature; never confirm whether anyone is home "
        "or will come sign right now, and don't say the delivery can't be "
        "accepted or turn the driver away"
    ),
    "food_delivery": (
        "a food delivery (pizza, takeout, DoorDash/Grubhub/Uber Eats, etc). "
        'Say something like "Thanks, I\'ll pass that along right away." '
        "-- unlike a package, food can't just be left at the door, so "
        "don't say to leave it by the door. Never confirm whether anyone "
        "is home or will come out to get it, don't add conditions like "
        "\"when they're ready,\" don't say deliveries aren't accepted or "
        "expected, and never invite the driver inside or offer to "
        "unlock/open the door"
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
        "someone up -- say something like \"Thanks, I'll pass that "
        'along." Keep it brief; never confirm whether anyone is '
        "currently home, ready, or will be coming out -- just that the "
        "message has been passed along"
    ),
    "visitation": (
        "a personal or social visit -- a friend, family member, or "
        "neighbor stopping by (not a stranger asking for someone by name, "
        'not a delivery or solicitor) -- say something like "Thanks, '
        "I'll pass that along!\" Keep it warm, not transactional; never "
        "confirm whether anyone is currently home or available, and never "
        'invite them inside or say anything like "come in" -- warmth '
        "doesn't mean granting entry"
    ),
    "service_appointment": (
        "a contractor or technician arriving for a scheduled service "
        "appointment. Say something like \"Thanks, I'll pass that "
        "along.\" The visitor IS the expected technician -- don't imply "
        "someone else (a separate tech, a representative) is coming to "
        "meet them or is still on the way; just acknowledge that their "
        "arrival has been noted. Don't confirm whether anyone is "
        "currently home"
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
        "the household's schedule, or offer to unlock/open the door -- and "
        'never invite the visitor inside or say anything like "come in"/'
        '"come on in"/"feel free to enter," even warmly or casually.'
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


# -- Tool-calling classification (opt-in, see Orchestrator.use_tool_calling) --
#
# Separate from `_classification_prompt` above: that prompt's "reply with
# only the category name" instruction is written for plain free-text output
# and doesn't make sense for a model that's supposed to call a function
# instead. `_classification_prompt`/`_notification_summary_prompt` are left
# completely unchanged -- they're still the automatic fallback whenever
# tool-calling is off or a tool-calling attempt fails.

_CLASSIFY_INTENT_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "classify_intent",
        "description": (
            "Record your top 1-3 best-matching categories for what this doorbell "
            "visitor wants, ranked by confidence. Always call this."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "top_3": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 3,
                    "items": {
                        "type": "object",
                        "properties": {
                            "category": {
                                "type": "string",
                                "enum": [*_LLM_CLASSIFIABLE_INTENTS, "unknown"],
                            },
                            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        },
                        "required": ["category", "confidence"],
                    },
                }
            },
            "required": ["top_3"],
        },
    },
}

_FLAG_FOR_REVIEW_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "flag_for_review",
        "description": (
            "Call this IN ADDITION TO classify_intent when you are not confident any "
            "single category clearly applies, so a household member can review it "
            "themselves instead of KNOCK guessing wrong."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": (
                        "One short phrase (under 12 words) summarizing who's at the "
                        "door and what they want, suitable for a phone notification."
                    ),
                }
            },
            "required": ["summary"],
        },
    },
}

_CLASSIFICATION_TOOLS: list[dict[str, Any]] = [_CLASSIFY_INTENT_TOOL, _FLAG_FOR_REVIEW_TOOL]

_EXTRACT_DETAILS_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "extract_notification_details",
        "description": (
            "Record a short notification summary plus any details the visitor actually "
            "stated, for a household push notification."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "Under 12 words, suitable for a phone push notification.",
                },
                "visitor_name": {"type": "string"},
                "organization": {"type": "string"},
                "stated_purpose": {"type": "string"},
                "reference_number": {"type": "string"},
            },
            "required": ["summary"],
        },
    },
}


def _tool_classification_messages(visitor_text: str) -> list[dict[str, str]]:
    labels = ", ".join(
        f"{name} ({_INTENT_CLASSIFICATION_HINTS[name]})" for name in _LLM_CLASSIFIABLE_INTENTS
    )
    user_content = (
        f'A visitor at the door said or triggered: "{visitor_text}"\n'
        "The system's keyword rules found no match. Call classify_intent with your "
        "top 1-3 best-matching categories (confidence 0-1 each), choosing only from: "
        f"{labels}, unknown. If you are not confident any single category clearly "
        "applies, also call flag_for_review with a short summary of what the visitor "
        "said, so a person can decide."
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


def _extract_details_messages(visitor_text: str, intent: str) -> list[dict[str, str]]:
    category = _INTENT_DESCRIPTIONS.get(intent, intent)
    user_content = (
        f'A visitor at the door said or triggered: "{visitor_text}"\n'
        f"This has been classified as: {category}.\n"
        "Call extract_notification_details with a short natural summary (under 12 "
        "words) suitable for a phone push notification, plus any of these details "
        "actually stated: the visitor's name, their organization/company, their "
        "stated purpose, and any reference number (tracking/appointment/badge). "
        "Leave a field out if it wasn't actually said -- never invent a name, "
        "company, or number that isn't there."
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


class IntentCandidate(NamedTuple):
    category: str
    confidence: float


class _IntentRefinement(NamedTuple):
    intent: str
    needs_review: bool
    review_candidates: list[str]
    review_summary: str | None


class NotificationDetails(BaseModel):
    """Structured result of `Orchestrator.extract_notification_details()` --
    `summary` is always present (the model writes it itself, or it's the
    plain-text fallback's `fallback`/generated sentence); the rest are only
    populated when the visitor actually stated them.
    """

    summary: str
    visitor_name: str | None = None
    organization: str | None = None
    stated_purpose: str | None = None
    reference_number: str | None = None


def _clean_optional_str(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _parse_classify_intent_candidates(result: ChatToolResult) -> list[IntentCandidate]:
    legal = {*_LLM_CLASSIFIABLE_INTENTS, "unknown"}
    for call in result.tool_calls:
        if call.name != "classify_intent":
            continue
        raw_items = call.arguments.get("top_3")
        if not isinstance(raw_items, list):
            continue
        parsed: list[IntentCandidate] = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            category = str(item.get("category", "")).strip().lower()
            confidence = item.get("confidence")
            if category in legal and isinstance(confidence, int | float):
                parsed.append(IntentCandidate(category=category, confidence=float(confidence)))
        if parsed:
            return sorted(parsed, key=lambda c: c.confidence, reverse=True)[:3]
    # No usable classify_intent tool call -- fall back to exact-matching the
    # model's own free-text content, i.e. today's existing safety net.
    guess = result.content.strip().lower()
    if guess in legal:
        return [IntentCandidate(category=guess, confidence=1.0)]
    return []


def _parse_flag_for_review(result: ChatToolResult) -> tuple[bool, str | None]:
    for call in result.tool_calls:
        if call.name == "flag_for_review":
            return True, _clean_optional_str(call.arguments.get("summary"))
    return False, None


class Orchestrator:
    def __init__(
        self,
        policy: PolicyEngine | None = None,
        audit_log: AuditLog | None = None,
        llm_provider: LLMProvider | None = None,
        use_tool_calling: bool = False,
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
        # Whether classify_intent/flag_for_review/extract_notification_details
        # use real structured tool-calling (see _refine_unknown_intent and
        # extract_notification_details) instead of plain free text. Resolved
        # by the caller (a bridge's main(), see OllamaConfig.use_tool_calling's
        # tri-state capability-detection story) -- this constructor just takes
        # the final bool, it doesn't do any detection itself. Only engaged for
        # a provider that actually offers `chat_with_tools` (duck-typed, not
        # an isinstance check, so a test fake doesn't need to subclass
        # OllamaProvider) -- any other provider silently keeps using the
        # plain-text path regardless of this flag.
        self.use_tool_calling = use_tool_calling
        ollama_config = getattr(llm_provider, "config", None)
        self._review_confidence_floor = getattr(
            ollama_config, "intent_review_confidence_floor", 0.5
        )
        self._review_confidence_margin = getattr(
            ollama_config, "intent_review_confidence_margin", 0.15
        )

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
            needs_review = False
            review_candidates: list[str] = []
            review_summary: str | None = None
            if intent == "unknown":
                refinement = self._refine_unknown_intent(event.text)
                intent = refinement.intent
                needs_review = refinement.needs_review
                review_candidates = refinement.review_candidates
                review_summary = refinement.review_summary
            last_intent = intent
            response_text = self.policy.apply_style(self._text_for_intent(intent, event.text))
            response_text = _with_greeting(response_text, is_first_turn=is_first_turn)
            response = ResponseDecision(
                text=response_text,
                safe=True,
                escalate=False,
                reason="normal",
                intent=last_intent,
                needs_review=needs_review,
                review_candidates=review_candidates,
                review_summary=review_summary,
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

    def _refine_unknown_intent(self, visitor_text: str) -> _IntentRefinement:
        """One more chance to recognize a known-but-unlisted situation (a
        contractor, someone asking for a person by name, an official
        visit, concerning-but-not-emergency behavior) before `classify_intent`
        truly gives up on a message.

        Tries tool-calling first when enabled (see `_refine_unknown_intent_via_tools`),
        falling back to the original plain-text exact-match path on any
        failure -- same best-effort pattern as `_text_for_intent`: no
        provider, a failure, or a reply outside `_LLM_CLASSIFIABLE_INTENTS`
        all fall back to "unknown", never a regression from today's
        existing behavior.
        """
        if self.llm_provider is None:
            return _IntentRefinement("unknown", False, [], None)

        if self.use_tool_calling and hasattr(self.llm_provider, "chat_with_tools"):
            try:
                return self._refine_unknown_intent_via_tools(visitor_text)
            except Exception as exc:  # noqa: BLE001 - best-effort, falls back below
                logger.warning(
                    "Tool-calling classification failed, falling back to plain text: %s", exc
                )

        try:
            guess = self.llm_provider.generate(_classification_prompt(visitor_text)).strip().lower()
        except Exception as exc:  # noqa: BLE001 - best-effort, falls back below
            logger.warning("LLM intent classification failed: %s", exc)
            return _IntentRefinement("unknown", False, [], None)

        result = guess if guess in _LLM_CLASSIFIABLE_INTENTS else "unknown"
        logger.info("LLM intent refinement: text=%r guess=%r -> %s", visitor_text, guess, result)
        return _IntentRefinement(result, False, [], None)

    def _refine_unknown_intent_via_tools(self, visitor_text: str) -> _IntentRefinement:
        result: ChatToolResult = self.llm_provider.chat_with_tools(  # type: ignore[union-attr]
            _tool_classification_messages(visitor_text), _CLASSIFICATION_TOOLS
        )
        candidates = _parse_classify_intent_candidates(result)
        model_flagged, review_summary = _parse_flag_for_review(result)

        if not candidates:
            # Neither a structured tool call nor free-text content matched
            # anything -- genuinely no usable signal. This *is* the
            # "unknown" notification gap: flag it for review rather than
            # silently dropping it the way today's plain-text path would.
            return _IntentRefinement("unknown", True, [], review_summary)

        top = candidates[0]
        second_confidence = candidates[1].confidence if len(candidates) > 1 else 0.0
        margin = top.confidence - second_confidence
        # The deterministic floor/margin check is the real trigger here --
        # the model's own flag_for_review call is just an additional OR'd
        # signal, so a model that never self-reports uncertainty still gets
        # caught by the numeric check, and a model that over-calls
        # flag_for_review can only affect notification routing, nothing else.
        uncertain = (
            top.confidence < self._review_confidence_floor
            or margin < self._review_confidence_margin
            or model_flagged
        )
        logger.info(
            "Tool-call intent classification: text=%r candidates=%r uncertain=%s",
            visitor_text,
            candidates,
            uncertain,
        )
        return _IntentRefinement(
            top.category, uncertain, [c.category for c in candidates], review_summary
        )

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

    def extract_notification_details(
        self, visitor_text: str, intent: str, *, fallback: str
    ) -> NotificationDetails:
        """A richer replacement for `summarize_for_notification()`: the same
        natural-language summary, plus structured fields (visitor name,
        organization, stated purpose, reference number) when the visitor
        actually stated them -- all from one tool call, not a second
        round-trip on top of classification/response generation. Only the
        bridges call this, and only once a notification is actually about
        to fire (section 8 of the plan), regardless of whether `intent` came
        from keyword rules or LLM refinement.

        Same best-effort pattern as every other LLM-backed method here: not
        enabled, or any failure, falls back to `summarize_for_notification`'s
        identical plain-text behavior.
        """
        if self.use_tool_calling and hasattr(self.llm_provider, "chat_with_tools"):
            try:
                return self._extract_notification_details_via_tools(visitor_text, intent)
            except Exception as exc:  # noqa: BLE001 - best-effort, falls back below
                logger.warning(
                    "Tool-calling notification-detail extraction failed, falling back: %s", exc
                )

        return NotificationDetails(
            summary=self.summarize_for_notification(visitor_text, fallback=fallback)
        )

    def _extract_notification_details_via_tools(
        self, visitor_text: str, intent: str
    ) -> NotificationDetails:
        result: ChatToolResult = self.llm_provider.chat_with_tools(  # type: ignore[union-attr]
            _extract_details_messages(visitor_text, intent), [_EXTRACT_DETAILS_TOOL]
        )
        for call in result.tool_calls:
            if call.name != "extract_notification_details":
                continue
            summary = _clean_optional_str(call.arguments.get("summary"))
            if summary is not None:
                return NotificationDetails(
                    summary=summary,
                    visitor_name=_clean_optional_str(call.arguments.get("visitor_name")),
                    organization=_clean_optional_str(call.arguments.get("organization")),
                    stated_purpose=_clean_optional_str(call.arguments.get("stated_purpose")),
                    reference_number=_clean_optional_str(call.arguments.get("reference_number")),
                )
        raise ValueError("no usable extract_notification_details tool call")
