import pytest
from fastapi.testclient import TestClient

from knock.api.app import app, get_audit_log, get_session_store
from knock.api.auth_routes import get_auth_store, get_web_session_store
from knock.core.audit import JSONLAuditLog
from knock.core.auth import AuthStore, WebSessionStore
from knock.core.session_store import JSONFileSessionStore


@pytest.fixture
def client(tmp_path):
    auth_store = AuthStore(tmp_path / "auth.json")
    web_session_store = WebSessionStore(tmp_path / "web_sessions.json")
    app.dependency_overrides[get_auth_store] = lambda: auth_store
    app.dependency_overrides[get_web_session_store] = lambda: web_session_store
    app.dependency_overrides[get_session_store] = lambda: JSONFileSessionStore(
        tmp_path / "sessions"
    )
    app.dependency_overrides[get_audit_log] = lambda: JSONLAuditLog(tmp_path / "audit.jsonl")
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _post(client: TestClient, url: str, json: dict | None = None):
    """POST with the double-submit CSRF header a real browser client would send.

    starlette-csrf sets a `csrftoken` cookie on every response; an unsafe
    request that already carries our session cookie must echo that value
    back as the `x-csrftoken` header, or it's rejected with 403 regardless
    of whether the caller is actually logged in correctly.
    """
    headers = {}
    token = client.cookies.get("csrftoken")
    if token:
        headers["x-csrftoken"] = token
    return client.post(url, json=json, headers=headers)


def _setup(client: TestClient, username: str = "jon", password: str = "a-good-password"):
    return _post(client, "/api/auth/setup", {"username": username, "password": password})


def test_status_reports_setup_required_with_no_users(client) -> None:
    resp = client.get("/api/auth/status")
    assert resp.status_code == 200
    assert resp.json() == {"setup_required": True}


def test_setup_creates_admin_and_logs_in(client) -> None:
    resp = _setup(client)

    assert resp.status_code == 201
    assert resp.json() == {"username": "jon"}
    assert "knock_session" in resp.cookies


def test_status_reports_setup_not_required_after_setup(client) -> None:
    _setup(client)

    resp = client.get("/api/auth/status")

    assert resp.json() == {"setup_required": False}


def test_setup_rejects_a_second_admin(client) -> None:
    _setup(client)

    resp = _setup(client, username="someone-else", password="another-password")

    assert resp.status_code == 409


def test_setup_rejects_a_short_password(client) -> None:
    resp = _setup(client, password="short")
    assert resp.status_code == 422


def test_login_succeeds_with_correct_credentials(client) -> None:
    _setup(client)

    resp = _post(client, "/api/auth/login", {"username": "jon", "password": "a-good-password"})

    assert resp.status_code == 200
    assert resp.json() == {"username": "jon"}
    assert "knock_session" in resp.cookies


def test_login_fails_with_wrong_password(client) -> None:
    _setup(client)

    resp = _post(client, "/api/auth/login", {"username": "jon", "password": "wrong"})

    assert resp.status_code == 401


def test_login_fails_for_unknown_user(client) -> None:
    resp = _post(client, "/api/auth/login", {"username": "nobody", "password": "whatever"})
    assert resp.status_code == 401


def test_me_requires_authentication(client) -> None:
    resp = client.get("/api/auth/me")
    assert resp.status_code == 401


def test_me_returns_current_user_when_logged_in(client) -> None:
    _setup(client)

    resp = client.get("/api/auth/me")

    assert resp.status_code == 200
    assert resp.json() == {"username": "jon"}


def test_logout_clears_the_session(client) -> None:
    _setup(client)

    logout_resp = _post(client, "/api/auth/logout")
    assert logout_resp.status_code == 204

    me_resp = client.get("/api/auth/me")
    assert me_resp.status_code == 401


def test_unsafe_request_with_a_session_but_no_csrf_token_is_rejected(client) -> None:
    _setup(client)

    resp = client.post("/api/auth/logout")  # no x-csrftoken header

    assert resp.status_code == 403


def test_existing_respond_endpoint_still_requires_no_auth(client) -> None:
    resp = client.post(
        "/respond",
        json={
            "source": "doorbell",
            "text": "Hi I have a package",
            "timestamp": "2026-01-01T12:00:00Z",
        },
    )
    assert resp.status_code == 200
