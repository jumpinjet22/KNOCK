"""Persisted, UI-editable settings -- the third config layer.

Every `*Config` class in `knock.config` already resolves settings via
`from_env()` (env var -> hardcoded default). This adds a layer in between:
a local JSON file the web UI can read and write, so settings survive a
restart without requiring an env var for every field. Precedence, applied
per-field by each `*Config.from_sources()` method: **env var wins, then
this store, then the hardcoded default** -- so nothing here can silently
override an operator's explicit env var, and nothing already deployed via
`.env`/systemd/compose needs to change.

This file can hold real credentials (API keys, tokens, broker passwords),
so it's written with `0600` permissions and should never be committed or
backed up alongside the repo -- same posture as `.env` already has, just
persisted and UI-editable instead of loaded once at process start.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

CONFIG_FORMAT_VERSION = 1
DEFAULT_CONFIG_PATH_ENV_VAR = "KNOCK_CONFIG_PATH"


def _default_config_path() -> Path:
    configured = os.environ.get(DEFAULT_CONFIG_PATH_ENV_VAR)
    if configured:
        return Path(configured)
    return Path.home() / ".local" / "share" / "knock" / "config.json"


class ConfigStore:
    """A single local JSON file holding every settings section.

    Shape on disk: `{"version": 1, "sections": {"ollama": {...}, ...}}`.
    One section per `*Config` class, keyed by a short lowercase name (e.g.
    "ollama", "vision", "mqtt", "frigate", "homeassistant", "unifi").
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else _default_config_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _read_raw(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": CONFIG_FORMAT_VERSION, "sections": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"version": CONFIG_FORMAT_VERSION, "sections": {}}
        if not isinstance(data, dict) or "sections" not in data:
            return {"version": CONFIG_FORMAT_VERSION, "sections": {}}
        return data

    def _write_raw(self, data: dict[str, Any]) -> None:
        data.setdefault("version", CONFIG_FORMAT_VERSION)
        tmp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        tmp_path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        tmp_path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600 -- this file can hold secrets
        os.replace(tmp_path, self.path)  # atomic on POSIX

    def get_section(self, name: str) -> dict[str, Any]:
        """Return the stored settings for one section, or `{}` if unset."""
        section = self._read_raw()["sections"].get(name)
        return section if isinstance(section, dict) else {}

    def set_section(self, name: str, values: dict[str, Any]) -> None:
        """Replace one section's stored settings wholesale."""
        data = self._read_raw()
        data["sections"][name] = values
        self._write_raw(data)

    def update_section(self, name: str, values: dict[str, Any]) -> None:
        """Merge `values` into one section's existing stored settings."""
        data = self._read_raw()
        section = data["sections"].get(name)
        merged = dict(section) if isinstance(section, dict) else {}
        merged.update(values)
        data["sections"][name] = merged
        self._write_raw(data)

    def all_sections(self) -> dict[str, dict[str, Any]]:
        """Return every stored section, for diagnostics/export."""
        return dict(self._read_raw()["sections"])
