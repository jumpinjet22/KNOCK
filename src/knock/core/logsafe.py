"""Logging text that someone else controls (a visitor's words, a model's
reply) without letting it forge log lines.

`repr()` already escapes line breaks in a string, so a `%r` log line is safe
in practice -- but an explicit replace also covers a non-string object whose
`repr` contains one, and it is the form static analysis (CodeQL's
log-injection query) recognizes as a sanitizer. The output is identical to
`%r` for ordinary strings, so existing log lines read the same.
"""

from __future__ import annotations


def log_safe(value: object) -> str:
    """`repr(value)` with any line break escaped; log it with `%s`."""
    return repr(value).replace("\r", "\\r").replace("\n", "\\n")
