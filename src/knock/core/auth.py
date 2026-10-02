"""Password auth + server-side web sessions for the web UI.

Deliberately separate from `knock.core.state.SessionState` (a *visitor*
conversation's turns) -- this module is about who's logged into the admin
web UI, an unrelated concept that happens to share the word "session."

No hand-rolled crypto: password hashing is Argon2id via `argon2-cffi` (the
current OWASP-recommended default), and web session tokens are
`secrets.token_urlsafe` (CSPRNG), never a value an attacker could predict
or forge. A session token is capability-bearing -- treat `auth.json` and
`web_sessions.json` with the same care as `config.json` (local-only,
`0600`, never committed).
"""

from __future__ import annotations

import json
import os
import secrets
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from pydantic import BaseModel

_hasher = PasswordHasher()

DEFAULT_AUTH_PATH_ENV_VAR = "KNOCK_AUTH_PATH"
DEFAULT_WEB_SESSION_PATH_ENV_VAR = "KNOCK_WEB_SESSION_PATH"
DEFAULT_PASSKEY_PATH_ENV_VAR = "KNOCK_PASSKEY_PATH"
SESSION_COOKIE_NAME = "knock_session"
SESSION_LIFETIME = timedelta(days=30)


def _default_path(env_var: str, filename: str) -> Path:
    configured = os.environ.get(env_var)
    if configured:
        return Path(configured)
    return Path.home() / ".local" / "share" / "knock" / filename


def _write_json_private(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(f"{path.suffix}.tmp")
    tmp_path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp_path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600
    os.replace(tmp_path, path)


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


class User(BaseModel):
    """A single admin account. Single-operator today -- one user is normal."""

    username: str
    password_hash: str
    created_at: datetime


class AuthStore:
    """Argon2-hashed user accounts, persisted as JSON (`0600`)."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = (
            Path(path)
            if path is not None
            else _default_path(DEFAULT_AUTH_PATH_ENV_VAR, "auth.json")
        )

    def _load(self) -> dict[str, User]:
        raw = _read_json(self.path)
        users = raw.get("users", {})
        return {name: User.model_validate(data) for name, data in users.items()}

    def _save(self, users: dict[str, User]) -> None:
        _write_json_private(
            self.path,
            {"users": {name: user.model_dump(mode="json") for name, user in users.items()}},
        )

    def has_any_user(self) -> bool:
        return len(self._load()) > 0

    def get_user(self, username: str) -> User | None:
        return self._load().get(username)

    def create_user(self, username: str, password: str) -> User:
        if self.get_user(username) is not None:
            raise ValueError(f"user already exists: {username!r}")
        user = User(
            username=username,
            password_hash=_hasher.hash(password),
            created_at=datetime.now(UTC),
        )
        users = self._load()
        users[username] = user
        self._save(users)
        return user

    def set_password(self, username: str, password: str) -> None:
        users = self._load()
        existing = users.get(username)
        if existing is None:
            raise ValueError(f"no such user: {username!r}")
        users[username] = existing.model_copy(update={"password_hash": _hasher.hash(password)})
        self._save(users)

    def verify_password(self, username: str, password: str) -> bool:
        user = self.get_user(username)
        if user is None:
            # Still run a hash operation so a nonexistent-vs-wrong-password
            # response doesn't leak which one happened via timing.
            _hasher.hash(password)
            return False
        try:
            _hasher.verify(user.password_hash, password)
        except VerifyMismatchError:
            return False
        return True


class WebSession(BaseModel):
    """One logged-in browser session."""

    token: str
    username: str
    created_at: datetime
    expires_at: datetime


class WebSessionStore:
    """Server-side session tokens, persisted as JSON (`0600`).

    The token itself lives in an httpOnly cookie (`SESSION_COOKIE_NAME`) --
    this store is what maps that opaque token back to a username, so a
    stolen/old token can be invalidated server-side (unlike a stateless
    signed cookie/JWT, which stays "valid" until it expires no matter what).
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = (
            Path(path)
            if path is not None
            else _default_path(DEFAULT_WEB_SESSION_PATH_ENV_VAR, "web_sessions.json")
        )

    def _load(self) -> dict[str, WebSession]:
        raw = _read_json(self.path)
        sessions = raw.get("sessions", {})
        return {token: WebSession.model_validate(data) for token, data in sessions.items()}

    def _save(self, sessions: dict[str, WebSession]) -> None:
        _write_json_private(
            self.path,
            {
                "sessions": {
                    token: session.model_dump(mode="json") for token, session in sessions.items()
                }
            },
        )

    def create(self, username: str) -> WebSession:
        now = datetime.now(UTC)
        session = WebSession(
            token=secrets.token_urlsafe(32),
            username=username,
            created_at=now,
            expires_at=now + SESSION_LIFETIME,
        )
        sessions = self._load()
        self._prune_expired(sessions)
        sessions[session.token] = session
        self._save(sessions)
        return session

    def get(self, token: str) -> WebSession | None:
        session = self._load().get(token)
        if session is None:
            return None
        if session.expires_at < datetime.now(UTC):
            self.delete(token)
            return None
        return session

    def delete(self, token: str) -> None:
        sessions = self._load()
        if token in sessions:
            del sessions[token]
            self._save(sessions)

    def _prune_expired(self, sessions: dict[str, WebSession]) -> None:
        now = datetime.now(UTC)
        for token in [t for t, s in sessions.items() if s.expires_at < now]:
            del sessions[token]


class PasskeyCredential(BaseModel):
    """One registered WebAuthn credential.

    `credential_id`/`public_key` are stored base64url-encoded (plain `str`,
    not `bytes`) purely so this round-trips through the same JSON
    persistence helpers as everything else here -- `py_webauthn` itself
    works in raw bytes, decoded/encoded at the store boundary.
    """

    credential_id: str
    public_key: str
    sign_count: int
    username: str
    nickname: str = ""
    created_at: datetime


class PasskeyStore:
    """WebAuthn credential storage, same JSON-file/`0600` pattern as `AuthStore`."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = (
            Path(path)
            if path is not None
            else _default_path(DEFAULT_PASSKEY_PATH_ENV_VAR, "passkeys.json")
        )

    def _load(self) -> dict[str, PasskeyCredential]:
        raw = _read_json(self.path)
        creds = raw.get("credentials", {})
        return {cred_id: PasskeyCredential.model_validate(data) for cred_id, data in creds.items()}

    def _save(self, creds: dict[str, PasskeyCredential]) -> None:
        _write_json_private(
            self.path,
            {
                "credentials": {
                    cred_id: cred.model_dump(mode="json") for cred_id, cred in creds.items()
                }
            },
        )

    def add(self, credential: PasskeyCredential) -> None:
        creds = self._load()
        creds[credential.credential_id] = credential
        self._save(creds)

    def get(self, credential_id: str) -> PasskeyCredential | None:
        return self._load().get(credential_id)

    def list_all(self) -> list[PasskeyCredential]:
        return list(self._load().values())

    def list_for_user(self, username: str) -> list[PasskeyCredential]:
        return [cred for cred in self.list_all() if cred.username == username]

    def update_sign_count(self, credential_id: str, sign_count: int) -> None:
        creds = self._load()
        existing = creds.get(credential_id)
        if existing is None:
            raise ValueError(f"no such credential: {credential_id!r}")
        creds[credential_id] = existing.model_copy(update={"sign_count": sign_count})
        self._save(creds)

    def delete(self, credential_id: str) -> None:
        creds = self._load()
        if credential_id in creds:
            del creds[credential_id]
            self._save(creds)
