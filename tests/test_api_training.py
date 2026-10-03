from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from knock.api.app import app
from knock.api.auth_routes import get_auth_store, get_web_session_store
from knock.api.training_routes import get_audit_log, get_review_store, get_script_runner
from knock.core.audit import AuditEntry, JSONLAuditLog
from knock.core.auth import AuthStore, WebSessionStore
from knock.core.script_runner import ScriptRunner
from knock.core.training import TrainingReview, TrainingReviewStore, example_key


@pytest.fixture
def audit_log(tmp_path) -> JSONLAuditLog:
    return JSONLAuditLog(tmp_path / "audit.jsonl")


@pytest.fixture
def review_store(tmp_path) -> TrainingReviewStore:
    return TrainingReviewStore(tmp_path / "training_reviews.json")


@pytest.fixture
def scripts_dir(tmp_path) -> Path:
    directory = tmp_path / "scripts"
    directory.mkdir()
    (directory / "generate_scenarios.py").write_text(
        "import sys\nprint('scenarios ran with', sys.argv[1:])"
    )
    (directory / "generate_training_data.py").write_text(
        "import sys\nprint('training data ran with', sys.argv[1:])"
    )
    return directory


@pytest.fixture
def script_runner(scripts_dir) -> ScriptRunner:
    return ScriptRunner(scripts_dir=scripts_dir)


@pytest.fixture
def client(tmp_path, audit_log, review_store, script_runner):
    app.dependency_overrides[get_auth_store] = lambda: AuthStore(tmp_path / "auth.json")
    app.dependency_overrides[get_web_session_store] = lambda: WebSessionStore(
        tmp_path / "web_sessions.json"
    )
    app.dependency_overrides[get_audit_log] = lambda: audit_log
    app.dependency_overrides[get_review_store] = lambda: review_store
    app.dependency_overrides[get_script_runner] = lambda: script_runner
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


def _post(client: TestClient, path: str, json: dict | None = None) -> httpx.Response:
    headers = {}
    token = client.cookies.get("csrftoken")
    if token:
        headers["x-csrftoken"] = token
    return client.post(path, json=json, headers=headers)


def _wait_until_idle(client: TestClient, *, timeout: float = 5.0) -> dict:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get("/api/training/scripts/status").json()
        if body["status"] != "running":
            return body
        time.sleep(0.02)
    raise AssertionError("script never finished")


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


# -- ui-mode ----------------------------------------------------------------


def test_ui_mode_requires_no_authentication(client) -> None:
    resp = client.get("/api/training/ui-mode")
    assert resp.status_code == 200


def test_ui_mode_defaults_to_full_ui(client, monkeypatch) -> None:
    monkeypatch.delenv("KNOCK_TRAINING_MODE", raising=False)
    resp = client.get("/api/training/ui-mode")
    assert resp.json() == {"training_only": False}


@pytest.mark.parametrize("value", ["1", "true", "True", "yes", "on"])
def test_ui_mode_recognizes_truthy_env_values(client, monkeypatch, value) -> None:
    monkeypatch.setenv("KNOCK_TRAINING_MODE", value)
    resp = client.get("/api/training/ui-mode")
    assert resp.json() == {"training_only": True}


def test_ui_mode_treats_empty_string_as_false(client, monkeypatch) -> None:
    monkeypatch.setenv("KNOCK_TRAINING_MODE", "0")
    resp = client.get("/api/training/ui-mode")
    assert resp.json() == {"training_only": False}


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


# -- scripts ----------------------------------------------------------------


def test_script_status_requires_authentication(client) -> None:
    resp = client.get("/api/training/scripts/status")
    assert resp.status_code == 401


def test_script_status_is_idle_before_anything_runs(client) -> None:
    _login(client)
    resp = client.get("/api/training/scripts/status")
    assert resp.status_code == 200
    assert resp.json() == {
        "script": None,
        "status": "idle",
        "exit_code": None,
        "started_at": None,
    }


def test_generate_scenarios_requires_authentication(client) -> None:
    resp = _post(client, "/api/training/scripts/generate-scenarios", {"models": ["m1"]})
    assert resp.status_code == 401


def test_generate_scenarios_rejects_empty_models(client) -> None:
    _login(client)
    resp = _post(client, "/api/training/scripts/generate-scenarios", {"models": []})
    assert resp.status_code == 400


def test_generate_scenarios_runs_and_completes(client) -> None:
    _login(client)
    resp = _post(
        client,
        "/api/training/scripts/generate-scenarios",
        {"models": ["m1", "m2"], "count_per_category": 3},
    )
    assert resp.status_code == 200
    assert resp.json()["script"] == "generate_scenarios"

    final = _wait_until_idle(client)
    assert final["status"] == "completed"
    assert final["exit_code"] == 0

    logs = client.get("/api/training/scripts/logs").json()
    assert any("--models" in line and "m1,m2" in line for line in logs["lines"])


def test_generate_training_data_runs_and_completes(client) -> None:
    _login(client)
    resp = _post(
        client,
        "/api/training/scripts/generate-training-data",
        {"models": ["m1"], "scenarios": "scripts/training_scenarios.txt"},
    )
    assert resp.status_code == 200
    assert resp.json()["script"] == "generate_training_data"

    final = _wait_until_idle(client)
    assert final["status"] == "completed"


def test_cannot_start_a_second_script_while_one_is_running(client, script_runner) -> None:
    _login(client)
    # Use a script that sleeps so the first run is still in-flight.
    (script_runner._scripts_dir / "generate_scenarios.py").write_text("import time\ntime.sleep(2)")
    resp1 = _post(client, "/api/training/scripts/generate-scenarios", {"models": ["m1"]})
    assert resp1.status_code == 200

    resp2 = _post(client, "/api/training/scripts/generate-training-data", {"models": ["m1"]})
    assert resp2.status_code == 409

    stop_resp = _post(client, "/api/training/scripts/stop")
    assert stop_resp.status_code == 200
    _wait_until_idle(client)


def test_stop_requires_authentication(client) -> None:
    resp = client.post("/api/training/scripts/stop")
    assert resp.status_code == 401


def test_script_logs_requires_authentication(client) -> None:
    resp = client.get("/api/training/scripts/logs")
    assert resp.status_code == 401
