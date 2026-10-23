"""Text that goes to the household -- a push notification, or a phone call
read aloud -- when some of it came from somewhere KNOCK doesn't control: a
visitor's words, a model's summary, text printed on a package label.

The danger is KNOCK's own voice lending trust to an attacker's message
("Urgent: your bank called, phone 555-0100"). So everything is forced onto
one short line, and a link or phone number is handled by channel:

  push  keeps it (the household can judge it) but says where it came from
  call  never reads it aloud; a link or number becomes "a link"/"a phone number"

Ordinary notifications -- short, no links or numbers -- come out unchanged.
"""

from __future__ import annotations

import re

PUSH_MAX_CHARS = 200
CALL_MAX_CHARS = 160

_URL = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
# A run of 7+ digits (spaces, dashes, dots, parentheses allowed between them)
# that is not part of a longer word, so camera IDs like "696ab4810067bb03" and
# "(tracking ...6784)" don't count.
_PHONE = re.compile(r"(?<!\w)\+?\d[\d\-. ()]{5,}\d(?!\w)")
# Zero-width and bidirectional-control characters: invisible in a notification,
# useful for hiding text or reordering it.
_INVISIBLE = re.compile("[​-‏‪-‮⁠⁦-⁩﻿]")

UNTRUSTED_CONTACT_NOTE = " (includes a link or number from the visitor -- don't trust it)"


def _collapse(text: object) -> str:
    return " ".join(_INVISIBLE.sub("", str(text)).split())


def one_line(text: object, limit: int) -> str:
    """`text` on one line, at most `limit` characters (ending in "..." if cut)."""
    collapsed = _collapse(text)
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: max(limit - 3, 0)].rstrip() + "..."


def has_contact(text: object) -> bool:
    """Whether `text` holds a link or something that looks like a phone number."""
    collapsed = _collapse(text)
    return bool(_URL.search(collapsed) or _PHONE.search(collapsed))


def for_push(message: object) -> str:
    """The notification text: one line, bounded, and flagged if it carries a
    link or phone number.
    """
    if has_contact(message):
        return one_line(message, PUSH_MAX_CHARS - len(UNTRUSTED_CONTACT_NOTE)) + (
            UNTRUSTED_CONTACT_NOTE
        )
    return one_line(message, PUSH_MAX_CHARS)


def for_call(message: object) -> str:
    """What is read aloud on a phone call: one line, bounded, with every link
    and phone number replaced so none is ever spoken.
    """
    text = _URL.sub("a link", _collapse(message))
    text = _PHONE.sub("a phone number", text)
    return one_line(text, CALL_MAX_CHARS)
