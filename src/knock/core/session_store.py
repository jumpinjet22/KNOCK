from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Protocol

from knock.core.state import SessionState

_SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

DEFAULT_SESSION_DIR_ENV_VAR = "KNOCK_SESSION_DIR"


def _default_session_dir() -> Path:
    configured = os.environ.get(DEFAULT_SESSION_DIR_ENV_VAR)
    if configured:
        return Path(configured)
    return Path.home() / ".local" / "share" / "knock" / "sessions"


class SessionStore(Protocol):
    """Persists `SessionState` so a conversation can resume across requests/processes."""

    def load(self, session_id: str) -> SessionState | None: ...

    def save(self, state: SessionState) -> None: ...

    def list_ids(self) -> list[str]: ...


class JSONFileSessionStore:
    """One JSON file per session, stored on local disk.

    Deliberately simple for a local-first, single-instance deployment -- a
    SQLite-backed store is a drop-in future upgrade behind the same
    `SessionStore` interface if concurrent access becomes a concern.
    """

    def __init__(self, directory: Path | str | None = None) -> None:
        self.directory = Path(directory) if directory is not None else _default_session_dir()
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, session_id: str) -> Path:
        if not _SAFE_SESSION_ID.match(session_id):
            raise ValueError(f"invalid session id: {session_id!r}")
        return self.directory / f"{session_id}.json"

    def load(self, session_id: str) -> SessionState | None:
        path = self._path(session_id)
        if not path.exists():
            return None
        return SessionState.model_validate_json(path.read_text())

    def save(self, state: SessionState) -> None:
        path = self._path(state.session_id)
        path.write_text(state.model_dump_json())

    def list_ids(self) -> list[str]:
        """Session ids, most recently updated first -- for a session browser.

        Sorts by each file's own mtime rather than parsing every session's
        `updated_at` (same information, far cheaper at any real volume of
        stored sessions).
        """
        paths = sorted(
            self.directory.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True
        )
        return [path.stem for path in paths]
