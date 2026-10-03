import os
import secrets
import stat
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Response
from fastapi.responses import FileResponse
from starlette.middleware.sessions import SessionMiddleware
from starlette_csrf import CSRFMiddleware

from knock.api.auth_routes import router as auth_router
from knock.api.debug_routes import router as debug_router
from knock.api.history_routes import router as history_router
from knock.api.oauth_routes import router as oauth_router
from knock.api.settings_routes import router as settings_router
from knock.api.supervisor_routes import get_bridge_supervisor
from knock.api.supervisor_routes import router as supervisor_router
from knock.api.video_routes import router as video_router
from knock.api.webauthn_routes import router as webauthn_router
from knock.config import OllamaConfig
from knock.core.audit import JSONLAuditLog
from knock.core.auth import SESSION_COOKIE_NAME
from knock.core.config_store import ConfigStore
from knock.core.events import VisitorEvent
from knock.core.orchestrator import Orchestrator
from knock.core.responses import ResponseDecision
from knock.core.session_store import JSONFileSessionStore
from knock.core.state import SessionState
from knock.providers.llm.ollama import OllamaProvider


def _get_or_create_secret(env_var: str, filename: str) -> str:
    """A stable secret, persisted across restarts.

    An env var always wins (lets a multi-process/container deployment pin
    one value); otherwise a random secret is generated once and reused from
    disk, rather than a fresh one each process start invalidating every
    outstanding cookie signed with the previous one.
    """
    env_value = os.environ.get(env_var)
    if env_value:
        return env_value

    path = Path.home() / ".local" / "share" / "knock" / filename
    if path.exists():
        return path.read_text(encoding="utf-8").strip()

    path.parent.mkdir(parents=True, exist_ok=True)
    value = secrets.token_urlsafe(32)
    path.write_text(value, encoding="utf-8")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return value


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Bring back whichever bridges are marked autostart -- a redeployed
    # container otherwise comes up with every bridge stopped until someone
    # manually starts each one again from the Processes page.
    get_bridge_supervisor().start_autostart_enabled()
    yield
    # Terminate every bridge subprocess on API shutdown -- paired with the
    # Dockerfile's `tini` entrypoint, since without an init process as PID 1,
    # `docker stop` only ever signals this process and these would be
    # orphaned rather than stopped.
    get_bridge_supervisor().shutdown_all()


app = FastAPI(title="KNOCK API", version="0.1.0", lifespan=_lifespan)
app.add_middleware(
    CSRFMiddleware,
    secret=_get_or_create_secret("KNOCK_CSRF_SECRET", "csrf_secret"),
    # Only enforced once a request already carries a login session cookie --
    # the pre-existing open API (/respond, /sessions/{id}) and the login/
    # setup routes themselves (no session cookie yet at that point) are
    # unaffected.
    sensitive_cookies={SESSION_COOKIE_NAME},
)
app.add_middleware(
    SessionMiddleware,
    secret_key=_get_or_create_secret("KNOCK_OAUTH_SESSION_SECRET", "oauth_session_secret"),
    # Only ever holds the OAuth `state`/`nonce` for the few seconds of a
    # Google sign-in redirect round-trip, not real session auth (that's
    # `knock_session`, set separately by `_issue_session`) -- so, unlike
    # that cookie, this one isn't worth making dynamically `Secure` per
    # request scheme.
    https_only=False,
    same_site="lax",
)
app.include_router(auth_router)
app.include_router(settings_router)
app.include_router(debug_router)
app.include_router(supervisor_router)
app.include_router(oauth_router)
app.include_router(video_router)
app.include_router(history_router)
app.include_router(webauthn_router)
# Resolved once at process start (same as the CSRF/OAuth-session secrets
# above) -- an Ollama setting changed later through the web UI takes effect
# on the next restart, not live. Construction itself never touches the
# network (OllamaProvider's httpx.Client is lazy), so a down/misconfigured
# Ollama server can't fail startup -- only the unknown-intent fallback
# degrades (see Orchestrator._text_for_intent).
orchestrator = Orchestrator(
    llm_provider=OllamaProvider(config=OllamaConfig.from_sources(ConfigStore()))
)


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


def _web_dist_dir() -> Path:
    configured = os.environ.get("KNOCK_WEB_DIST")
    if configured:
        return Path(configured)
    # Relative to the process's cwd -- matches the existing convention of
    # running uvicorn from the repo root locally, or from the Docker image's
    # WORKDIR (where `web/dist` is copied alongside `src/`).
    return Path("web/dist")


_web_dist = _web_dist_dir()

if _web_dist.is_dir():
    # A true SPA catch-all, not `StaticFiles(html=True)` (which only serves
    # index.html at "/" itself, not for client-side routes -- a direct
    # request to e.g. /login or /settings would 404 rather than loading the
    # app and letting React Router take over). Registered last, after every
    # real API route above, so this only ever catches what nothing else
    # matched. Skipped entirely if the frontend hasn't been built (e.g. in
    # CI, or a backend-only checkout) rather than crashing the whole app.
    _web_dist_resolved = _web_dist.resolve()

    @app.get("/{full_path:path}", include_in_schema=False)
    def serve_web(full_path: str) -> FileResponse:
        # full_path is raw, attacker-controlled URL input -- resolve and
        # confirm it's still inside the dist dir before ever serving it, or
        # a path like "../../../../etc/passwd" would escape it.
        candidate = (_web_dist_resolved / full_path).resolve()
        if full_path and candidate.is_relative_to(_web_dist_resolved) and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(_web_dist_resolved / "index.html")
