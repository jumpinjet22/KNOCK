from __future__ import annotations

import logging
import re

from pydantic import BaseModel, Field

from knock.conversation.rules import RuleSet
from knock.core.session import ConversationContext

logger = logging.getLogger(__name__)

ESCALATE_REASON = "emergency"
BLOCK_REASON = "blocked_request"
NORMAL_REASON = "normal"

# Deterministic backstop, same pattern as providers/vision/safety.py's
# alarming-language filter: the LLM response prompt already instructs
# "never offer to unlock/open the door," but a visitor can phrase an entry
# request in ways that dodge that literal wording ("mind letting me
# inside?") while the model still responds with an actual invitation
# ("feel free to come in!") rather than unlocking anything literally --
# found via adversarial testing. A false positive here (suppressing a
# genuinely benign reply that happens to contain one of these phrases) is
# the acceptable failure mode -- per this project's safety-first stance,
# a boring fallback beats a response that invites a stranger inside.
_ENTRY_INVITATION_PHRASES = (
    "come in",
    "come inside",
    "come on in",
    "you can enter",
    "feel free to enter",
    "i'll let you in",
    "i will let you in",
    "head on in",
    "go ahead and come",
)

# "I'll let them/the homeowner know you're here" (or any variant: "you
# stopped by", "you arrived", "you need a signature") confirms to the
# visitor that a specific person exists and is being told, in real time,
# that they're present -- exactly the occupancy-confirmation this whole
# system exists to prevent. This is not a one-off: it was the single
# dominant rejection reason across an entire night's worth of synthetic
# training-data review (every teacher model produced it, not just one).
# "I'll pass that along" (addressed generically, not "to" anyone
# specific) is the safe relay pattern and is NOT caught by this.
#
# A fixed noun list ("them"/"the homeowner"/"the person"...) turned out
# not to be enough -- cross-model testing surfaced "the resident," "the
# family," "whoever is inside," "the appropriate person," and there's no
# reason to believe that list is now complete either. This matches the
# *structure* instead: "let" ... "know" with a short, bounded gap, which
# catches any noun phrase in between regardless of wording. Excludes "let
# you know" specifically -- addressed back to the visitor themselves (e.g.
# about a future signature), that's the established *safe* relay pattern,
# not a third-party disclosure.
_LET_SOMEONE_KNOW_RE = re.compile(r"\blet\s+(?!you\b)(?:\S+\s+){0,4}know\b", re.IGNORECASE)

_OCCUPANCY_CONFIRMATION_PHRASES = (
    "they'll come",
    "they will come",
    "come down",
    "come out",
    "come grab",
    "come sign",
    "step out",
    "person inside",
    "someone inside",
    "whoever is inside",
    "whoever's inside",
    "in the house",
    "in the home",
)

SAFE_RESPONSE_FALLBACK = "Thanks, I'll pass that along."


def _contains_unsafe_disclosure(text: str) -> bool:
    lowered = text.lower()
    if _LET_SOMEONE_KNOW_RE.search(lowered):
        return True
    return any(
        phrase in lowered
        for phrase in (*_ENTRY_INVITATION_PHRASES, *_OCCUPANCY_CONFIRMATION_PHRASES)
    )


class PolicyDecision(BaseModel):
    """Result of evaluating a visitor's text against the active rule set."""

    allowed: bool
    reason: str
    flags: list[str] = Field(default_factory=list)
    confidence: dict[str, float] = Field(default_factory=dict)
    matched_rule_ids: list[str] = Field(default_factory=list)


class PolicyEngine:
    """Safety-first policy checks, driven by a data-defined rule set (see `rules.json`)."""

    def __init__(self, rule_set: RuleSet | None = None) -> None:
        self.rule_set = rule_set or RuleSet.default()

    def evaluate(self, text: str) -> PolicyDecision:
        lowered = text.lower()
        confidence: dict[str, float] = {}
        matched_rule_ids: list[str] = []

        for rule in self.rule_set.rules:
            if any(phrase in lowered for phrase in rule.phrases):
                confidence[rule.flag] = min(1.0, confidence.get(rule.flag, 0.0) + rule.weight)
                matched_rule_ids.append(rule.id)

        flags = sorted(
            flag for flag, score in confidence.items() if score >= self.rule_set.threshold
        )

        escalate_flags = {rule.flag for rule in self.rule_set.rules if rule.action == "escalate"}
        if any(flag in escalate_flags for flag in flags):
            reason = ESCALATE_REASON
            allowed = True
        elif flags:
            reason = BLOCK_REASON
            allowed = False
        else:
            reason = NORMAL_REASON
            allowed = True

        return PolicyDecision(
            allowed=allowed,
            reason=reason,
            flags=flags,
            confidence=confidence,
            matched_rule_ids=matched_rule_ids,
        )

    def apply_style(self, response: str) -> str:
        styled = response.strip()[:140]
        if _contains_unsafe_disclosure(styled):
            logger.warning("Response flagged as an unsafe disclosure, suppressing: %r", styled)
            return SAFE_RESPONSE_FALLBACK
        return styled

    def build_context(self, text: str, intent: str, flags: list[str]) -> ConversationContext:
        return ConversationContext(visitor_text=text, intent=intent, policy_flags=flags)
