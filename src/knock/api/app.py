import os
import secrets
import stat
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Response
from starlette_csrf import CSRFMiddleware

from knock.api.auth_routes import router as auth_router
from knock.core.audit import JSONLAuditLog
from knock.core.auth import SESSION_COOKIE_NAME
from knock.core.events import VisitorEvent
from knock.core.orchestrator import Orchestrator
from knock.core.responses import ResponseDecision
from knock.core.session_store import JSONFileSessionStore
from knock.core.state import SessionState


def _get_or_create_csrf_secret() -> str:
    """A stable secret for signing CSRF tokens, persisted across restarts.

    An env var always wins (lets a multi-process/container deployment pin
    one value); otherwise a random secret is generated once and reused from
    disk, rather than a fresh one each process start invalidating every
    outstanding CSRF cookie on every restart.
    """
    env_value = os.environ.get("KNOCK_CSRF_SECRET")
    if env_value:
        return env_value

    path = Path.home() / ".local" / "share" / "knock" / "csrf_secret"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()

    path.parent.mkdir(parents=True, exist_ok=True)
    value = secrets.token_urlsafe(32)
    path.write_text(value, encoding="utf-8")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return value


app = FastAPI(title="KNOCK API", version="0.1.0")
app.add_middleware(
    CSRFMiddleware,
    secret=_get_or_create_csrf_secret(),
    # Only enforced once a request already carries a login session cookie --
    # the pre-existing open API (/respond, /sessions/{id}) and the login/
    # setup routes themselves (no session cookie yet at that point) are
    # unaffected.
    sensitive_cookies={SESSION_COOKIE_NAME},
)
app.include_router(auth_router)
orchestrator = Orchestrator()


def get_session_store() -> JSONFileSessionStore:
    return JSONFileSessionStore()


def get_audit_log() -> JSONLAuditLog:
    return JSONLAuditLog()


SessionStoreDep = Annotated[JSONFileSessionStore, Depends(get_session_store)]
AuditLogDep = Annotated[JSONLAuditLog, Depends(get_audit_log)]


@app.post("/respond", response_model=ResponseDecision)
def respond(
    event: VisitorEvent,
    response: Response,
    *,
    session_id: str | None = None,
    store: SessionStoreDep,
    audit_log: AuditLogDep,
) -> ResponseDecision:
    """Answer a visitor event, persisting conversation state across calls.

    Pass `session_id` on subsequent calls (returned here as the `X-Session-Id`
    response header) to continue the same conversation instead of starting a
    new one.
    """
    resolved_session_id = session_id or uuid.uuid4().hex
    try:
        state = store.load(resolved_session_id) or SessionState(
            session_id=resolved_session_id, updated_at=datetime.now(UTC)
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    decision = orchestrator.respond(event, state=state, audit_log=audit_log)
    store.save(state)

    response.headers["X-Session-Id"] = resolved_session_id
    return decision


@app.get("/sessions/{session_id}", response_model=SessionState)
def get_session(session_id: str, *, store: SessionStoreDep) -> SessionState:
    """Debug endpoint: inspect a persisted session's state."""
    try:
        state = store.load(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if state is None:
        raise HTTPException(status_code=404, detail="session not found")
    return state
