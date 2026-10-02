import json

import pytest
from fastapi.testclient import TestClient

from knock.api.app import app, get_audit_log, get_session_store
from knock.core.audit import JSONLAuditLog
from knock.core.session_store import JSONFileSessionStore


@pytest.fixture
def audit_log_path(tmp_path):
    return tmp_path / "audit.jsonl"


@pytest.fixture
def client(tmp_path, audit_log_path):
    app.dependency_overrides[get_session_store] = lambda: JSONFileSessionStore(tmp_path)
    app.dependency_overrides[get_audit_log] = lambda: JSONLAuditLog(audit_log_path)
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _payload(text: str) -> dict:
    return {"source": "doorbell", "text": text, "timestamp": "2026-01-01T12:00:00Z"}


def test_respond_without_session_id_generates_one(client) -> None:
    resp = client.post("/respond", json=_payload("Hi I have a package"))

    assert resp.status_code == 200
    assert "leave the package" in resp.json()["text"].lower()
    assert resp.headers["X-Session-Id"]


def test_respond_persists_state_across_calls(client) -> None:
    first = client.post("/respond", json=_payload("Hi I have a package"))
    session_id = first.headers["X-Session-Id"]

    second = client.post("/respond", json=_payload("still here"), params={"session_id": session_id})

    assert second.headers["X-Session-Id"] == session_id

    session = client.get(f"/sessions/{session_id}")
    assert session.status_code == 200
    assert session.json()["turn_count"] == 2


def test_get_unknown_session_returns_404(client) -> None:
    resp = client.get("/sessions/does-not-exist")
    assert resp.status_code == 404


def test_get_session_with_unsafe_id_returns_400(client) -> None:
    resp = client.get("/sessions/bad id!")
    assert resp.status_code == 400


def test_respond_appends_an_audit_entry(client, audit_log_path) -> None:
    client.post("/respond", json=_payload("Hi I have a package"))

    lines = audit_log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["text"] == "Hi I have a package"
    assert entry["intent"] == "delivery"
