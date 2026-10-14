"""Local record of package arrivals and acknowledgements.

Deliberately separate from the conversation audit log (`core/audit.py`):
that file is the source for the Training page's fine-tuning export, and a
"package detected" row isn't a visitor turn -- it has no speech, no policy
decision, and no response to learn from.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel

DEFAULT_PACKAGE_LOG_ENV_VAR = "KNOCK_PACKAGE_LOG"

# detected: Protect reported a package. notified: the household notification
# went out (possibly held back until the visitor left). acknowledged: someone
# tapped "Got it".
PackageEventKind = Literal["detected", "notified", "acknowledged"]


class PackageEvent(BaseModel):
    timestamp: datetime
    device_id: str
    kind: PackageEventKind
    # Only meaningful for "detected": whether someone was still at the door,
    # i.e. whether the notification was held until they left.
    visitor_present: bool | None = None


class PackageLog(Protocol):
    def record(self, event: PackageEvent) -> None: ...


class NullPackageLog:
    """No-op default, same reasoning as `NullAuditLog`."""

    def record(self, event: PackageEvent) -> None:
        return None


def _default_package_log_path() -> Path:
    configured = os.environ.get(DEFAULT_PACKAGE_LOG_ENV_VAR)
    if configured:
        return Path(configured)
    return Path.home() / ".local" / "share" / "knock" / "package_events.jsonl"


class JSONLPackageLog:
    """Append-only, local-only JSON-lines file."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else _default_package_log_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, event: PackageEvent) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(event.model_dump_json())
            handle.write("\n")

    def recent(self, limit: int = 200) -> list[PackageEvent]:
        """The most recent `limit` events, newest first."""
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()
        events = [PackageEvent.model_validate_json(line) for line in lines if line.strip()]
        events.reverse()
        return events[:limit]
