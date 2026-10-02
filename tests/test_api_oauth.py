import pytest
from authlib.integrations.base_client import OAuthError
from fastapi.testclient import TestClient

from knock.api.app import app
from knock.api.auth_routes import get_auth_store, get_web_session_store
from knock.api.oauth_routes import GoogleOAuthSettings, get_google_client_factory
from knock.api.settings_routes import get_config_store
from knock.core.auth import AuthStore, WebSessionStore
from knock.core.config_store import ConfigStore


class _FakeGoogleClient:
    def __init__(self, token: dict | None = None, raise_error: OAuthError | None = None) -> None:
        self.token = token
        self.raise_error = raise_error
        self.redirect_calls: list[str] = []

    async def authorize_redirect(self, request, redirect_uri: str):
        self.redirect_calls.append(redirect_uri)
        from fastapi.responses import RedirectResponse

        return RedirectResponse(url="https://accounts.google.com/fake-consent-screen")

    async def authorize_access_token(self, request):
        if self.raise_error is not None:
            raise self.raise_error
        return self.token


@pytest.fixture
def config_store(tmp_path) -> ConfigStore:
    return ConfigStore(tmp_path / "config.json")


@pytest.fixture
def client(tmp_path, config_store):
    app.dependency_overrides[get_auth_store] = lambda: AuthStore(tmp_path / "auth.json")
    app.dependency_overrides[get_web_session_store] = lambda: WebSessionStore(
        tmp_path / "web_sessions.json"
    )
    app.dependency_overrides[get_config_store] = lambda: config_store
    try:
        yield TestClient(app, follow_redirects=False)
    finally:
        app.dependency_overrides.clear()


def _post(client: TestClient, url: str, json: dict):
    headers = {}
    token = client.cookies.get("csrftoken")
    if token:
        headers["x-csrftoken"] = token
    return client.post(url, json=json, headers=headers)


def _put(client: TestClient, url: str, json: dict):
    headers = {}
    token = client.cookies.get("csrftoken")
    if token:
        headers["x-csrftoken"] = token
    return client.put(url, json=json, headers=headers)


def _login(client: TestClient) -> None:
    resp = _post(client, "/api/auth/setup", {"username": "jon", "password": "a-good-password"})
    assert resp.status_code == 201


def _configure_google(config_store: ConfigStore, allowed_email: str = "jon@example.com") -> None:
    config_store.update_section(
        "oauth_google",
        {
            "client_id": "abc.apps.googleusercontent.com",
            "client_secret": "shh",
            "allowed_email": allowed_email,
        },
    )


def _with_fake_client(fake: _FakeGoogleClient) -> None:
    app.dependency_overrides[get_google_client_factory] = lambda: lambda settings: fake


# -- status (public) ---------------------------------------------------------------


def test_google_status_is_false_when_unconfigured(client) -> None:
    resp = client.get("/api/oauth/google/status")
    assert resp.status_code == 200
    assert resp.json() == {"configured": False}


def test_google_status_is_true_once_fully_configured(client, config_store) -> None:
    _configure_google(config_store)
    resp = client.get("/api/oauth/google/status")
    assert resp.json() == {"configured": True}


# -- config (authenticated) ---------------------------------------------------------


def test_get_google_config_requires_authentication(client) -> None:
    resp = client.get("/api/oauth/google/config")
    assert resp.status_code == 401


def test_update_google_config_requires_authentication(client) -> None:
    resp = _put(client, "/api/oauth/google/config", {"client_id": "x"})
    assert resp.status_code == 401


def test_update_then_get_google_config_round_trips(client, config_store) -> None:
    _login(client)
    resp = _put(
        client,
        "/api/oauth/google/config",
        {
            "client_id": "abc.apps.googleusercontent.com",
            "client_secret": "shh",
            "allowed_email": "jon@example.com",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["client_id"] == "abc.apps.googleusercontent.com"
    assert body["has_client_secret"] is True
    assert body["allowed_email"] == "jon@example.com"
    assert body["configured"] is True

    resp = client.get("/api/oauth/google/config")
    assert resp.json() == body


def test_blank_client_secret_leaves_existing_secret_unchanged(client, config_store) -> None:
    _login(client)
    _configure_google(config_store)

    resp = _put(client, "/api/oauth/google/config", {"allowed_email": "someone-else@example.com"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["has_client_secret"] is True
    assert body["allowed_email"] == "someone-else@example.com"


# -- login --------------------------------------------------------------------------


def test_google_login_requires_configuration(client) -> None:
    resp = client.get("/api/oauth/google/login")
    assert resp.status_code == 400


def test_google_login_redirects_to_google(client, config_store) -> None:
    _configure_google(config_store)
    fake = _FakeGoogleClient()
    _with_fake_client(fake)

    resp = client.get("/api/oauth/google/login")

    assert resp.status_code in (302, 307)
    assert fake.redirect_calls, "expected authorize_redirect to have been called"


# -- callback ---------------------------------------------------------------------


def test_google_callback_issues_a_session_for_the_allowed_email(client, config_store) -> None:
    _configure_google(config_store, allowed_email="jon@example.com")
    fake = _FakeGoogleClient(
        token={"userinfo": {"email": "jon@example.com", "email_verified": True}}
    )
    _with_fake_client(fake)

    resp = client.get("/api/oauth/google/callback")

    assert resp.status_code in (302, 307)
    assert "knock_session" in resp.cookies

    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json() == {"username": "jon@example.com"}


def test_google_callback_rejects_an_email_outside_the_allow_list(client, config_store) -> None:
    _configure_google(config_store, allowed_email="jon@example.com")
    fake = _FakeGoogleClient(
        token={"userinfo": {"email": "someone-else@example.com", "email_verified": True}}
    )
    _with_fake_client(fake)

    resp = client.get("/api/oauth/google/callback")

    assert resp.status_code == 403
    assert "knock_session" not in resp.cookies


def test_google_callback_rejects_unverified_email(client, config_store) -> None:
    _configure_google(config_store, allowed_email="jon@example.com")
    fake = _FakeGoogleClient(
        token={"userinfo": {"email": "jon@example.com", "email_verified": False}}
    )
    _with_fake_client(fake)

    resp = client.get("/api/oauth/google/callback")

    assert resp.status_code == 401


def test_google_callback_surfaces_oauth_errors_as_401(client, config_store) -> None:
    _configure_google(config_store)
    fake = _FakeGoogleClient(raise_error=OAuthError(description="state mismatch"))
    _with_fake_client(fake)

    resp = client.get("/api/oauth/google/callback")

    assert resp.status_code == 401


def test_google_oauth_settings_defaults_are_blank() -> None:
    settings = GoogleOAuthSettings()
    assert settings.client_id == ""
    assert settings.client_secret.get_secret_value() == ""
    assert settings.allowed_email == ""
