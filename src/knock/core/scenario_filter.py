"""Cheap, deterministic checks on a synthetic "visitor line" before it is
sent to the (slow, LLM-based) voice check or into the training pipeline.

In a real review 821 of 1,478 rejected examples were voice vetoes: the
generating model wrote the homeowner's side of the conversation, or its own
preamble ("Here are five realistic things a visitor might say:"), instead of
something a visitor would say. These patterns catch the common shapes.
"""

import re

_PREAMBLE = re.compile(
    r"^\s*(?:here (?:are|is)|sure[,!]? here|okay[,!]? here|below (?:are|is)|"
    r"examples?\b|scenarios?\b|visitor (?:lines?|says?)\b)",
    re.IGNORECASE,
)
_LIST_MARKER = re.compile(r"^\s*(?:\d+[.)]|[-*•])\s+")

# The homeowner (or the assistant) talking to the visitor, not the reverse.
_HOMEOWNER_VOICE = re.compile(
    r"\b(?:let (?:them|him|her) know i(?:'m| am)|i(?:'ll| will) (?:tell|let) (?:them|him|her)|"
    r"i(?:'ll| will) be (?:at|right at|down) the door|i(?:'m| am) coming (?:to|down)|"
    r"we(?:'re| are) good\b|no,? thanks?,? we (?:don't|do not)|we (?:don't|do not) "
    r"(?:take|want|accept|need)\b|just leave it (?:by|at|on)|"
    r"drop it off (?:on|at|by)|thanks,? i(?:'ll| will) (?:grab|get|take) it|"
    r"leave it (?:by|at|on) the (?:door|porch)[,.]? thanks)",
    re.IGNORECASE,
)


def scenario_is_junk(line: str) -> bool:
    """True for a line that is clearly not something a visitor would say."""
    text = line.strip().strip("\"'")
    if not text:
        return True
    if text.endswith(":") or _PREAMBLE.search(text) or _LIST_MARKER.search(line):
        return True
    return bool(_HOMEOWNER_VOICE.search(text))
