from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from knock.api.app import app
from knock.api.auth_routes import get_auth_store, get_web_session_store
from knock.api.history_routes import get_audit_log, get_session_store
from knock.core.audit import AuditEntry, JSONLAuditLog
from knock.core.auth import AuthStore, WebSessionStore
from knock.core.session_store import JSONFileSessionStore
from knock.core.state import SessionState


@pytest.fixture
def audit_log(tmp_path) -> JSONLAuditLog:
    return JSONLAuditLog(tmp_path / "audit.jsonl")


@pytest.fixture
def session_store(tmp_path) -> JSONFileSessionStore:
    return JSONFileSessionStore(tmp_path / "sessions")


@pytest.fixture
def client(tmp_path, audit_log, session_store):
    app.dependency_overrides[get_auth_store] = lambda: AuthStore(tmp_path / "auth.json")
    app.dependency_overrides[get_web_session_store] = lambda: WebSessionStore(
        tmp_path / "web_sessions.json"
    )
    app.dependency_overrides[get_audit_log] = lambda: audit_log
    app.dependency_overrides[get_session_store] = lambda: session_store
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _login(client: TestClient) -> None:
    headers = {}
    token = client.cookies.get("csrftoken")
    if token:
        headers["x-csrftoken"] = token
    resp = client.post(
        "/api/auth/setup",
        json={"username": "jon", "password": "a-good-password"},
        headers=headers,
    )
    assert resp.status_code == 201


def _audit_entry(text: str, intent: str | None = "delivery") -> AuditEntry:
    return AuditEntry(
        timestamp=datetime.now(UTC),
        text=text,
        matched_flags={},
        matched_rule_ids=[],
        allowed=True,
        reason="normal",
        intent=intent,
    )


# -- auth gating ----------------------------------------------------------------


def test_get_audit_requires_authentication(client) -> None:
    resp = client.get("/api/audit")
    assert resp.status_code == 401


def test_list_sessions_requires_authentication(client) -> None:
    resp = client.get("/api/sessions")
    assert resp.status_code == 401


def test_get_stats_requires_authentication(client) -> None:
    resp = client.get("/api/stats")
    assert resp.status_code == 401


# -- audit ------------------------------------------------------------------------


def test_get_audit_returns_entries_newest_first(client, audit_log) -> None:
    _login(client)
    audit_log.record(_audit_entry("first"))
    audit_log.record(_audit_entry("second"))

    resp = client.get("/api/audit")

    assert resp.status_code == 200
    texts = [entry["text"] for entry in resp.json()]
    assert texts == ["second", "first"]


def test_get_audit_respects_limit_query_param(client, audit_log) -> None:
    _login(client)
    audit_log.record(_audit_entry("first"))
    audit_log.record(_audit_entry("second"))
    audit_log.record(_audit_entry("third"))

    resp = client.get("/api/audit", params={"limit": 1})

    assert resp.status_code == 200
    assert [entry["text"] for entry in resp.json()] == ["third"]


def test_get_audit_is_empty_before_anything_is_recorded(client) -> None:
    _login(client)
    resp = client.get("/api/audit")
    assert resp.status_code == 200
    assert resp.json() == []


# -- stats ------------------------------------------------------------------------


def test_get_stats_tallies_tracked_intents(client, audit_log) -> None:
    _login(client)
    audit_log.record(_audit_entry("one", intent="soliciting"))
    audit_log.record(_audit_entry("two", intent="soliciting"))
    audit_log.record(_audit_entry("three", intent="religious_soliciting"))
    audit_log.record(_audit_entry("four", intent="emergency"))  # never tracked

    resp = client.get("/api/stats")

    assert resp.status_code == 200
    counts = resp.json()["counts"]
    assert counts["soliciting"] == 2
    assert counts["religious_soliciting"] == 1
    assert counts["political_soliciting"] == 0
    assert "emergency" not in counts


def test_get_stats_is_all_zero_before_anything_is_recorded(client) -> None:
    _login(client)
    resp = client.get("/api/stats")
    assert resp.status_code == 200
    counts = resp.json()["counts"]
    assert all(value == 0 for value in counts.values())


# -- sessions ---------------------------------------------------------------------


def test_list_sessions_returns_saved_states(client, session_store) -> None:
    _login(client)
    session_store.save(SessionState(session_id="abc123", turn_count=2, last_intent="delivery"))

    resp = client.get("/api/sessions")

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["session_id"] == "abc123"
    assert body[0]["turn_count"] == 2
    assert body[0]["last_intent"] == "delivery"


def test_list_sessions_respects_limit_query_param(client, session_store) -> None:
    _login(client)
    session_store.save(SessionState(session_id="one"))
    session_store.save(SessionState(session_id="two"))

    resp = client.get("/api/sessions", params={"limit": 1})

    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_list_sessions_is_empty_when_none_exist(client) -> None:
    _login(client)
    resp = client.get("/api/sessions")
    assert resp.status_code == 200
    assert resp.json() == []
