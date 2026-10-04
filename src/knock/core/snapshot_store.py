"""Short-lived, single-use, unguessable-token-addressed storage for a
doorbell snapshot photo attached to a household notification (see
unifi.py's _build_snapshot_url and api/snapshot_routes.py).

Disk-backed, not in-memory: UnifiBridge runs as its own OS subprocess
(spawned by core/supervisor.py), separate from the FastAPI process that
serves snapshot_routes.py -- an in-memory dict can't be shared across that
process boundary, so this follows the same local-disk pattern already used
by JSONFileSessionStore/WebSessionStore for the same reason.

Security model: no authentication at all (Home Assistant's mobile app
fetches a notification's image attachment directly from the phone, outside
KNOCK's own session system, and cannot present a session cookie). Safety
instead comes from: (1) secrets.token_urlsafe(32) -- 256 bits of CSPRNG
entropy, not guessable; (2) a short TTL (DEFAULT_TTL); (3) single-use --
`take()` deletes the file on its first successful read, so even a token
leaked via a phone's notification history stops working after one
legitimate fetch.
"""

from __future__ import annotations

import os
import re
import secrets
import time
from datetime import timedelta
from pathlib import Path

DEFAULT_SNAPSHOT_DIR_ENV_VAR = "KNOCK_SNAPSHOT_DIR"
DEFAULT_TTL = timedelta(minutes=10)
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_-]{16,64}$")


def _default_snapshot_dir() -> Path:
    configured = os.environ.get(DEFAULT_SNAPSHOT_DIR_ENV_VAR)
    if configured:
        return Path(configured)
    return Path.home() / ".local" / "share" / "knock" / "snapshots"


class SnapshotStore:
    def __init__(self, directory: Path | str | None = None, ttl: timedelta = DEFAULT_TTL) -> None:
        self.directory = Path(directory) if directory is not None else _default_snapshot_dir()
        self.directory.mkdir(parents=True, exist_ok=True)
        self._ttl_seconds = ttl.total_seconds()

    def put(self, image: bytes) -> str:
        self._prune_expired()
        token = secrets.token_urlsafe(32)
        (self.directory / f"{token}.jpg").write_bytes(image)
        return token

    def take(self, token: str) -> bytes | None:
        """Single-use: returns the image and deletes it, or None if the
        token is malformed, unknown, or expired.
        """
        if not _SAFE_TOKEN.match(token):
            return None
        path = self.directory / f"{token}.jpg"
        if not path.exists():
            return None
        try:
            age = time.time() - path.stat().st_mtime
            if age > self._ttl_seconds:
                path.unlink(missing_ok=True)
                return None
            data = path.read_bytes()
            path.unlink(missing_ok=True)
            return data
        except OSError:
            return None

    def _prune_expired(self) -> None:
        """Best-effort sweep of any snapshot that was never fetched (e.g.
        Home Assistant never loaded the notification's image) -- called on
        every `put()` so the directory can't grow unbounded on a device
        that runs for months.
        """
        now = time.time()
        for path in self.directory.glob("*.jpg"):
            try:
                if now - path.stat().st_mtime > self._ttl_seconds:
                    path.unlink(missing_ok=True)
            except OSError:
                continue
