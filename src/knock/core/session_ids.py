"""Session IDs, built the same way for every connector.

A session ID is also a filename in `JSONFileSessionStore`, so anything that
isn't a letter, digit, `_` or `-` is replaced -- an ID taken from a camera
or entity name can never carry a path separator. Capped at 128 characters.
"""

from __future__ import annotations

import re

_UNSAFE_SESSION_CHARS = re.compile(r"[^A-Za-z0-9_-]")


def build_session_id(prefix: str, raw: str) -> str:
    """`<prefix>-<sanitized raw>`; an empty `raw` becomes `unknown`."""
    safe = _UNSAFE_SESSION_CHARS.sub("_", raw) or "unknown"
    return f"{prefix}-{safe}"[:128]
