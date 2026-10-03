from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from knock.api.app import app
from knock.api.auth_routes import get_auth_store, get_web_session_store
from knock.api.training_routes import get_audit_log, get_review_store
from knock.core.audit import AuditEntry, JSONLAuditLog
from knock.core.auth import AuthStore, WebSessionStore
from knock.core.training import TrainingReview, TrainingReviewStore, example_key


@pytest.fixture
def audit_log(tmp_path) -> JSONLAuditLog:
    return JSONLAuditLog(tmp_path / "audit.jsonl")


@pytest.fixture
def review_store(tmp_path) -> TrainingReviewStore:
    return TrainingReviewStore(tmp_path / "training_reviews.json")


@pytest.fixture
def client(tmp_path, audit_log, review_store):
    app.dependency_overrides[get_auth_store] = lambda: AuthStore(tmp_path / "auth.json")
    app.dependency_overrides[get_web_session_store] = lambda: WebSessionStore(
        tmp_path / "web_sessions.json"
    )
    app.dependency_overrides[get_audit_log] = lambda: audit_log
    app.dependency_overrides[get_review_store] = lambda: review_store
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


def _put(client: TestClient, path: str, json: dict) -> httpx.Response:
    headers = {}
    token = client.cookies.get("csrftoken")
    if token:
        headers["x-csrftoken"] = token
    return client.put(path, json=json, headers=headers)


def _entry(
    text: str = "I'm here to work on your AC unit",
    response_text: str = "Thanks, I'll let them know you're here for the appointment.",
    intent: str | None = "service_appointment",
) -> AuditEntry:
    return AuditEntry(
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        text=text,
        response_text=response_text,
        matched_flags={},
        matched_rule_ids=[],
        allowed=True,
        reason="normal",
        intent=intent,
    )


# -- auth gating ----------------------------------------------------------------


def test_get_queue_requires_authentication(client) -> None:
    resp = client.get("/api/training/queue")
    assert resp.status_code == 401


def test_put_review_requires_authentication(client) -> None:
    resp = client.put("/api/training/queue/abc123", json={"status": "approved"})
    assert resp.status_code == 401


def test_export_requires_authentication(client) -> None:
    resp = client.get("/api/training/export")
    assert resp.status_code == 401


# -- queue ----------------------------------------------------------------


def test_queue_is_empty_before_anything_is_recorded(client) -> None:
    _login(client)
    resp = client.get("/api/training/queue")
    assert resp.status_code == 200
    assert resp.json() == []


def test_queue_excludes_entries_with_no_intent(client, audit_log) -> None:
    _login(client)
    audit_log.record(_entry(intent=None))

    resp = client.get("/api/training/queue")

    assert resp.status_code == 200
    assert resp.json() == []


def test_queue_lists_entries_newest_first_with_pending_review_status(client, audit_log) -> None:
    _login(client)
    audit_log.record(_entry(text="first"))
    audit_log.record(_entry(text="second"))

    resp = client.get("/api/training/queue")

    assert resp.status_code == 200
    body = resp.json()
    assert [item["entry"]["text"] for item in body] == ["second", "first"]
    assert all(item["review"]["status"] == "pending" for item in body)


def test_queue_reflects_a_saved_review(client, audit_log, review_store) -> None:
    _login(client)
    entry = _entry()
    audit_log.record(entry)
    review_store.set(example_key(entry), TrainingReview(status="approved"))

    resp = client.get("/api/training/queue")

    assert resp.status_code == 200
    assert resp.json()[0]["review"]["status"] == "approved"


# -- review ----------------------------------------------------------------


def test_put_review_saves_approval(client, audit_log) -> None:
    _login(client)
    entry = _entry()
    audit_log.record(entry)
    key = example_key(entry)

    resp = _put(client, f"/api/training/queue/{key}", {"status": "approved"})

    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"

    queue = client.get("/api/training/queue").json()
    assert queue[0]["review"]["status"] == "approved"


def test_put_review_saves_corrections(client, audit_log) -> None:
    _login(client)
    entry = _entry(intent="delivery")
    audit_log.record(entry)
    key = example_key(entry)

    resp = _put(
        client,
        f"/api/training/queue/{key}",
        {
            "status": "approved",
            "intent_override": "food_delivery",
            "response_override": "Corrected reply.",
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["intent_override"] == "food_delivery"
    assert body["response_override"] == "Corrected reply."


def test_put_review_rejects_an_invalid_intent_override(client) -> None:
    _login(client)
    resp = _put(
        client,
        "/api/training/queue/abc123",
        {"status": "approved", "intent_override": "not_a_real_intent"},
    )
    assert resp.status_code == 400


# -- intents ----------------------------------------------------------------


def test_get_intents_requires_authentication(client) -> None:
    resp = client.get("/api/training/intents")
    assert resp.status_code == 401


def test_get_intents_includes_unknown_and_service_appointment(client) -> None:
    _login(client)
    resp = client.get("/api/training/intents")
    assert resp.status_code == 200
    intents = resp.json()["intents"]
    assert "unknown" in intents
    assert "service_appointment" in intents
    assert "occupancy_probe" not in intents  # outside the closed classification-prompt list


# -- export ----------------------------------------------------------------


def test_export_is_empty_before_anything_is_approved(client, audit_log) -> None:
    _login(client)
    audit_log.record(_entry())

    resp = client.get("/api/training/export")

    assert resp.status_code == 200
    assert resp.text == ""
    assert resp.headers["content-type"].startswith("application/jsonl")
    assert "knock_training_data.jsonl" in resp.headers["content-disposition"]


def test_export_includes_an_approved_entrys_training_records(client, audit_log) -> None:
    _login(client)
    entry = _entry()
    audit_log.record(entry)
    key = example_key(entry)
    _put(client, f"/api/training/queue/{key}", {"status": "approved"})

    resp = client.get("/api/training/export")

    assert resp.status_code == 200
    lines = [line for line in resp.text.strip().splitlines() if line]
    assert len(lines) == 2
    assert all('"task"' in line for line in lines)
