"""Login/logout/me + first-run admin setup for the web UI.

Session auth only -- this is unrelated to `POST /respond`/`GET
/sessions/{id}`, which stay open per the web UI plan (no secrets flow
through them). `require_auth` is the dependency every new settings/
supervisor/debug route (later phases) is gated behind.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from knock.core.auth import SESSION_COOKIE_NAME, SESSION_LIFETIME, AuthStore, WebSessionStore

router = APIRouter(prefix="/api/auth", tags=["auth"])

MIN_PASSWORD_LENGTH = 8


def get_auth_store() -> AuthStore:
    return AuthStore()


def get_web_session_store() -> WebSessionStore:
    return WebSessionStore()


AuthStoreDep = Annotated[AuthStore, Depends(get_auth_store)]
WebSessionStoreDep = Annotated[WebSessionStore, Depends(get_web_session_store)]


def require_auth(
    *,
    knock_session: Annotated[str | None, Cookie()] = None,
    session_store: WebSessionStoreDep,
) -> str:
    """FastAPI dependency gating any route behind a logged-in session.

    Returns the logged-in username on success; raises 401 otherwise. A
    missing/expired/unknown token are all the same 401 to the caller --
    the distinction only matters server-side (see `WebSessionStore.get`).
    """
    if knock_session is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    session = session_store.get(knock_session)
    if session is None:
        raise HTTPException(status_code=401, detail="session expired or invalid")
    return session.username


CurrentUserDep = Annotated[str, Depends(require_auth)]


class SetupRequest(BaseModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=MIN_PASSWORD_LENGTH)


class LoginRequest(BaseModel):
    username: str
    password: str


class StatusResponse(BaseModel):
    setup_required: bool


class MeResponse(BaseModel):
    username: str


def _issue_session(
    request: Request, response: Response, session_store: WebSessionStore, username: str
) -> None:
    session = session_store.create(username)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session.token,
        httponly=True,
        # Adapts to how this request actually arrived -- plain http (local/
        # LAN dev, the common case today) still gets a usable cookie,
        # while a direct https deployment gets a real secure cookie. Behind
        # a reverse proxy, run uvicorn with --proxy-headers so this reflects
        # the proxy's scheme, not the plain-http hop to uvicorn itself.
        secure=request.url.scheme == "https",
        samesite="lax",
        max_age=int(SESSION_LIFETIME.total_seconds()),
        path="/",
    )


@router.get("/status")
def auth_status(*, auth_store: AuthStoreDep) -> StatusResponse:
    """Whether first-run setup (no admin account yet) is needed."""
    return StatusResponse(setup_required=not auth_store.has_any_user())


@router.post("/setup", status_code=201)
def setup(
    body: SetupRequest,
    request: Request,
    response: Response,
    *,
    auth_store: AuthStoreDep,
    session_store: WebSessionStoreDep,
) -> MeResponse:
    """First-run only: create the single admin account and log in as them."""
    if auth_store.has_any_user():
        raise HTTPException(status_code=409, detail="an admin account already exists")
    auth_store.create_user(body.username, body.password)
    _issue_session(request, response, session_store, body.username)
    return MeResponse(username=body.username)


@router.post("/login")
def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    *,
    auth_store: AuthStoreDep,
    session_store: WebSessionStoreDep,
) -> MeResponse:
    if not auth_store.verify_password(body.username, body.password):
        raise HTTPException(status_code=401, detail="invalid username or password")
    _issue_session(request, response, session_store, body.username)
    return MeResponse(username=body.username)


@router.post("/logout", status_code=204)
def logout(
    response: Response,
    *,
    session_store: WebSessionStoreDep,
    knock_session: Annotated[str | None, Cookie()] = None,
) -> None:
    if knock_session:
        session_store.delete(knock_session)
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")


@router.get("/me")
def me(current_user: CurrentUserDep) -> MeResponse:
    return MeResponse(username=current_user)
