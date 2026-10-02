"""Backstop filter for vision descriptions.

Prompting a vision model to avoid speculation (see `VisionConfig.prompt`)
helps, but it's not a guarantee -- models still sometimes hallucinate
alarming content ("possibly a bomb") out of an ambiguous shape. This is a
second, deterministic layer: anything a vision provider's raw output says
gets scanned here before it's allowed into a response a visitor (or a
household member) might see or hear. A false positive (suppressing a
genuinely benign description) is the correct failure mode -- per the
project's safety-first stance, a boring fallback beats a speculative,
alarming one.

This does not replace real threat detection or escalation; it only keeps
a vision model's unreliable self-generated language from being surfaced
verbatim. Anything actually safety-critical still goes through the
deterministic, auditable `PolicyEngine` on the visitor's own words.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

SAFE_FALLBACK_DESCRIPTION = "Something is visible at the door."

# Deliberately broad and over-inclusive -- see module docstring on why
# false positives are the acceptable failure mode here.
_ALARMING_TERMS = (
    "bomb",
    "explosive",
    "detonat",
    "weapon",
    "firearm",
    "gun",
    "rifle",
    "pistol",
    "grenade",
    "knife",
    "blade",
    "hostage",
    "terroris",
    "suicide",
    "kill",
)


def contains_alarming_language(description: str) -> bool:
    """Whether `description` mentions anything from the alarming-terms list."""
    lowered = description.lower()
    return any(term in lowered for term in _ALARMING_TERMS)


def sanitize_description(description: str) -> str:
    """Return `description` unchanged, or a safe fallback if it reads as alarming.

    Call this on every raw vision-model output before it reaches a
    VisitorEvent/response -- never skip it just because a particular
    provider or prompt "should" already be safe.
    """
    if contains_alarming_language(description):
        logger.warning(
            "Vision description flagged as alarming/speculative, suppressing: %r",
            description,
        )
        return SAFE_FALLBACK_DESCRIPTION
    return description
