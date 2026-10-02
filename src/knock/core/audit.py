from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel

DEFAULT_AUDIT_LOG_ENV_VAR = "KNOCK_AUDIT_LOG"


class AuditEntry(BaseModel):
    """One record of a policy/response decision, for the local audit trail."""

    timestamp: datetime
    text: str
    matched_flags: dict[str, float] = {}
    matched_rule_ids: list[str] = []
    allowed: bool
    reason: str
    intent: str | None = None


class AuditLog(Protocol):
    """Sink for `AuditEntry` records."""

    def record(self, entry: AuditEntry) -> None: ...


class NullAuditLog:
    """No-op sink -- the default unless a real audit log is explicitly wired in.

    Keeps direct unit tests and library usage side-effect-free; the actual
    API and CLI entry points opt into `JSONLAuditLog` instead.
    """

    def record(self, entry: AuditEntry) -> None:
        return None


def _default_audit_log_path() -> Path:
    configured = os.environ.get(DEFAULT_AUDIT_LOG_ENV_VAR)
    if configured:
        return Path(configured)
    return Path.home() / ".local" / "share" / "knock" / "audit.jsonl"


class JSONLAuditLog:
    """Append-only, local-only JSON-lines audit trail.

    This file can contain raw visitor speech, so treat it as sensitive --
    nothing in KNOCK transmits it anywhere; it is purely for local review.
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else _default_audit_log_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, entry: AuditEntry) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(entry.model_dump_json())
            handle.write("\n")
