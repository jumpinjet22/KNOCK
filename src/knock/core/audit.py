from __future__ import annotations

import os
from collections import Counter
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

    def recent(self, limit: int = 200) -> list[AuditEntry]:
        """The most recent `limit` entries, newest first.

        A plain linear scan of the whole file -- fine at local-first
        volumes; this isn't meant to scale to a high-traffic, multi-tenant
        deployment.
        """
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()
        entries = [AuditEntry.model_validate_json(line) for line in lines if line.strip()]
        entries.reverse()
        return entries[:limit]

    def count_by_intent(self) -> dict[str, int]:
        """All-time counts of each classified intent (e.g. for a dashboard's
        "N solicitors turned away" stats), across the whole file -- not
        bounded by `recent()`'s limit. `None` (emergency/blocked requests,
        which never reach `classify_intent()`) is excluded; those have
        their own `reason`-based meaning, not an intent to tally here.
        """
        if not self.path.exists():
            return {}
        lines = self.path.read_text(encoding="utf-8").splitlines()
        counts: Counter[str] = Counter()
        for line in lines:
            if not line.strip():
                continue
            entry = AuditEntry.model_validate_json(line)
            if entry.intent is not None:
                counts[entry.intent] += 1
        return dict(counts)
