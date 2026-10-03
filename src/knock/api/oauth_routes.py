"""OIDC sign-in: KNOCK as an OAuth *client*, not a provider. Bring-your-own
client_id/client_secret -- there's no single pre-registered OAuth app that
could work across every self-hosted instance's own hostname -- plus a
single allowed email, since a provider-verified identity has no other
connection to this install's one admin account otherwise.

Two providers today, Google and Authentik, both handled the same way:
Authlib's discovery-document support (`server_metadata_url`) gets the
authorization/token/JWKS endpoints and ID-token validation for free from
any OIDC-compliant provider. Google's discovery URL is fixed; Authentik is
self-hosted, so its discovery URL is built from an admin-provided issuer
URL instead. A non-OIDC provider (e.g. plain GitHub OAuth2) would mean
hand-specifying token/userinfo endpoints -- a reasonable future addition,
but a real, separate piece of work, not something to guess the exact
shape of here.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

from authlib.integrations.base_client import OAuthError
from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, SecretStr

from knock.api.auth_routes import CurrentUserDep, WebSessionStoreDep, _issue_session
from knock.api.settings_routes import ConfigStoreDep
from knock.core.config_store import ConfigStore

router = APIRouter(prefix="/api/oauth", tags=["oauth"])

GOOGLE_SERVER_METADATA_URL = "https://accounts.google.com/.well-known/openid-configuration"


def _verified_allowed_email(
    userinfo: dict[str, Any], allowed_email: str, provider_label: str
) -> str:
    """Shared safety check behind every provider's callback: the identity
    must be a verified email, and it must match this install's one
    allowed account -- a provider-verified identity has no other link to
    KNOCK's admin account otherwise.
    """
    email = userinfo.get("email")
    if not email or not userinfo.get("email_verified"):
        raise HTTPException(
            status_code=401, detail=f"{provider_label} did not return a verified email address"
        )
    if email.lower() != allowed_email.lower():
        raise HTTPException(
            status_code=403, detail=f"this {provider_label} account is not authorized"
        )
    return email


class GoogleOAuthSettings(BaseModel):
    client_id: str = ""
    client_secret: SecretStr = SecretStr("")
    allowed_email: str = ""


def _load_settings(store: ConfigStore) -> GoogleOAuthSettings:
    section = store.get_section("oauth_google")
    return GoogleOAuthSettings(
        client_id=section.get("client_id", ""),
        client_secret=section.get("client_secret", "") or "",
        allowed_email=section.get("allowed_email", ""),
    )


def _is_configured(settings: GoogleOAuthSettings) -> bool:
    return bool(
        settings.client_id and settings.client_secret.get_secret_value() and settings.allowed_email
    )


def _default_google_client_factory(settings: GoogleOAuthSettings) -> Any:
    oauth = OAuth()
    oauth.register(
        name="google",
        client_id=settings.client_id,
        client_secret=settings.client_secret.get_secret_value(),
        server_metadata_url=GOOGLE_SERVER_METADATA_URL,
        client_kwargs={"scope": "openid email"},
    )
    return oauth.google


def get_google_client_factory() -> Callable[[GoogleOAuthSettings], Any]:
    return _default_google_client_factory


GoogleClientFactoryDep = Annotated[
    Callable[[GoogleOAuthSettings], Any], Depends(get_google_client_factory)
]


# -- config: read/update the single Google OAuth app registration -------------------


class OAuthStatusResponse(BaseModel):
    configured: bool


class OAuthConfigResponse(BaseModel):
    client_id: str
    has_client_secret: bool
    allowed_email: str
    configured: bool


class UpdateOAuthConfigRequest(BaseModel):
    client_id: str | None = None
    client_secret: str | None = None
    allowed_email: str | None = None


def _config_response(settings: GoogleOAuthSettings) -> OAuthConfigResponse:
    return OAuthConfigResponse(
        client_id=settings.client_id,
        has_client_secret=bool(settings.client_secret.get_secret_value()),
        allowed_email=settings.allowed_email,
        configured=_is_configured(settings),
    )


@router.get("/google/status", response_model=OAuthStatusResponse)
def get_google_oauth_status(store: ConfigStoreDep) -> OAuthStatusResponse:
    # Public, unauthenticated (same as /api/auth/status) -- the login page
    # needs to know whether to offer a "Sign in with Google" button before
    # anyone is logged in. Deliberately reveals nothing but a bool.
    return OAuthStatusResponse(configured=_is_configured(_load_settings(store)))


@router.get("/google/config", response_model=OAuthConfigResponse)
def get_google_oauth_config(
    current_user: CurrentUserDep, *, store: ConfigStoreDep
) -> OAuthConfigResponse:
    return _config_response(_load_settings(store))


@router.put("/google/config", response_model=OAuthConfigResponse)
def update_google_oauth_config(
    body: UpdateOAuthConfigRequest, current_user: CurrentUserDep, *, store: ConfigStoreDep
) -> OAuthConfigResponse:
    updates: dict[str, Any] = {}
    if body.client_id is not None:
        updates["client_id"] = body.client_id
    if body.client_secret:  # blank/omitted -- leave the stored secret unchanged
        updates["client_secret"] = body.client_secret
    if body.allowed_email is not None:
        updates["allowed_email"] = body.allowed_email
    store.update_section("oauth_google", updates)
    return _config_response(_load_settings(store))


# -- login/callback -------------------------------------------------------------------


@router.get("/google/login")
async def google_login(
    request: Request, *, store: ConfigStoreDep, client_factory: GoogleClientFactoryDep
) -> Any:
    settings = _load_settings(store)
    if not _is_configured(settings):
        raise HTTPException(status_code=400, detail="Google sign-in is not configured")

    client = client_factory(settings)
    redirect_uri = str(request.url_for("google_oauth_callback"))
    return await client.authorize_redirect(request, redirect_uri)


@router.get("/google/callback", name="google_oauth_callback")
async def google_callback(
    request: Request,
    *,
    store: ConfigStoreDep,
    session_store: WebSessionStoreDep,
    client_factory: GoogleClientFactoryDep,
) -> RedirectResponse:
    settings = _load_settings(store)
    if not _is_configured(settings):
        raise HTTPException(status_code=400, detail="Google sign-in is not configured")

    client = client_factory(settings)
    try:
        token = await client.authorize_access_token(request)
    except OAuthError as exc:
        raise HTTPException(status_code=401, detail=f"Google sign-in failed: {exc}") from exc

    userinfo = token.get("userinfo") or {}
    email = _verified_allowed_email(userinfo, settings.allowed_email, "Google")

    redirect = RedirectResponse(url="/")
    _issue_session(request, redirect, session_store, email)
    return redirect


# -- Authentik -----------------------------------------------------------------------
#
# Self-hosted, so there's no fixed discovery URL like Google's -- the admin
# provides their own instance's issuer URL instead, and the discovery
# document is built from that (see `_authentik_metadata_url`).


class AuthentikOAuthSettings(BaseModel):
    issuer_url: str = ""
    client_id: str = ""
    client_secret: SecretStr = SecretStr("")
    allowed_email: str = ""


def _authentik_metadata_url(issuer_url: str) -> str:
    return issuer_url.rstrip("/") + "/.well-known/openid-configuration"


def _load_authentik_settings(store: ConfigStore) -> AuthentikOAuthSettings:
    section = store.get_section("oauth_authentik")
    return AuthentikOAuthSettings(
        issuer_url=section.get("issuer_url", ""),
        client_id=section.get("client_id", ""),
        client_secret=section.get("client_secret", "") or "",
        allowed_email=section.get("allowed_email", ""),
    )


def _is_authentik_configured(settings: AuthentikOAuthSettings) -> bool:
    return bool(
        settings.issuer_url
        and settings.client_id
        and settings.client_secret.get_secret_value()
        and settings.allowed_email
    )


def _default_authentik_client_factory(settings: AuthentikOAuthSettings) -> Any:
    oauth = OAuth()
    oauth.register(
        name="authentik",
        client_id=settings.client_id,
        client_secret=settings.client_secret.get_secret_value(),
        server_metadata_url=_authentik_metadata_url(settings.issuer_url),
        client_kwargs={"scope": "openid email"},
    )
    return oauth.authentik


def get_authentik_client_factory() -> Callable[[AuthentikOAuthSettings], Any]:
    return _default_authentik_client_factory


AuthentikClientFactoryDep = Annotated[
    Callable[[AuthentikOAuthSettings], Any], Depends(get_authentik_client_factory)
]


class AuthentikOAuthConfigResponse(BaseModel):
    issuer_url: str
    client_id: str
    has_client_secret: bool
    allowed_email: str
    configured: bool


class UpdateAuthentikOAuthConfigRequest(BaseModel):
    issuer_url: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    allowed_email: str | None = None


def _authentik_config_response(settings: AuthentikOAuthSettings) -> AuthentikOAuthConfigResponse:
    return AuthentikOAuthConfigResponse(
        issuer_url=settings.issuer_url,
        client_id=settings.client_id,
        has_client_secret=bool(settings.client_secret.get_secret_value()),
        allowed_email=settings.allowed_email,
        configured=_is_authentik_configured(settings),
    )


@router.get("/authentik/status", response_model=OAuthStatusResponse)
def get_authentik_oauth_status(store: ConfigStoreDep) -> OAuthStatusResponse:
    # Public, unauthenticated -- same reasoning as Google's status route.
    return OAuthStatusResponse(configured=_is_authentik_configured(_load_authentik_settings(store)))


@router.get("/authentik/config", response_model=AuthentikOAuthConfigResponse)
def get_authentik_oauth_config(
    current_user: CurrentUserDep, *, store: ConfigStoreDep
) -> AuthentikOAuthConfigResponse:
    return _authentik_config_response(_load_authentik_settings(store))


@router.put("/authentik/config", response_model=AuthentikOAuthConfigResponse)
def update_authentik_oauth_config(
    body: UpdateAuthentikOAuthConfigRequest, current_user: CurrentUserDep, *, store: ConfigStoreDep
) -> AuthentikOAuthConfigResponse:
    updates: dict[str, Any] = {}
    if body.issuer_url is not None:
        updates["issuer_url"] = body.issuer_url
    if body.client_id is not None:
        updates["client_id"] = body.client_id
    if body.client_secret:  # blank/omitted -- leave the stored secret unchanged
        updates["client_secret"] = body.client_secret
    if body.allowed_email is not None:
        updates["allowed_email"] = body.allowed_email
    store.update_section("oauth_authentik", updates)
    return _authentik_config_response(_load_authentik_settings(store))


@router.get("/authentik/login")
async def authentik_login(
    request: Request, *, store: ConfigStoreDep, client_factory: AuthentikClientFactoryDep
) -> Any:
    settings = _load_authentik_settings(store)
    if not _is_authentik_configured(settings):
        raise HTTPException(status_code=400, detail="Authentik sign-in is not configured")

    client = client_factory(settings)
    redirect_uri = str(request.url_for("authentik_oauth_callback"))
    return await client.authorize_redirect(request, redirect_uri)


@router.get("/authentik/callback", name="authentik_oauth_callback")
async def authentik_callback(
    request: Request,
    *,
    store: ConfigStoreDep,
    session_store: WebSessionStoreDep,
    client_factory: AuthentikClientFactoryDep,
) -> RedirectResponse:
    settings = _load_authentik_settings(store)
    if not _is_authentik_configured(settings):
        raise HTTPException(status_code=400, detail="Authentik sign-in is not configured")

    client = client_factory(settings)
    try:
        token = await client.authorize_access_token(request)
    except OAuthError as exc:
        raise HTTPException(status_code=401, detail=f"Authentik sign-in failed: {exc}") from exc

    userinfo = token.get("userinfo") or {}
    email = _verified_allowed_email(userinfo, settings.allowed_email, "Authentik")

    redirect = RedirectResponse(url="/")
    _issue_session(request, redirect, session_store, email)
    return redirect
