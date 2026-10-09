from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from knock.api.app import app
from knock.api.auth_routes import get_auth_store, get_web_session_store
from knock.api.training_routes import (
    get_audit_log,
    get_metadata_store,
    get_review_store,
    get_script_runner,
)
from knock.core.audit import AuditEntry, JSONLAuditLog
from knock.core.auth import AuthStore, WebSessionStore
from knock.core.script_runner import ScriptRunner
from knock.core.training import (
    TrainingMetadata,
    TrainingMetadataStore,
    TrainingReview,
    TrainingReviewStore,
    example_key,
)
from knock.core.training_judge import AggregatedJudgeResult


@pytest.fixture
def audit_log(tmp_path) -> JSONLAuditLog:
    return JSONLAuditLog(tmp_path / "audit.jsonl")


@pytest.fixture
def review_store(tmp_path) -> TrainingReviewStore:
    return TrainingReviewStore(tmp_path / "training_reviews.json")


@pytest.fixture
def metadata_store(tmp_path) -> TrainingMetadataStore:
    return TrainingMetadataStore(tmp_path / "training_metadata.json")


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
    (directory / "judge_training_data.py").write_text(
        "import sys\nprint('judge ran with', sys.argv[1:])"
    )
    (directory / "correct_training_data.py").write_text(
        "import sys\nprint('correct ran with', sys.argv[1:])"
    )
    return directory


@pytest.fixture
def script_runner(scripts_dir) -> ScriptRunner:
    return ScriptRunner(scripts_dir=scripts_dir)


@pytest.fixture
def client(tmp_path, audit_log, review_store, metadata_store, script_runner):
    app.dependency_overrides[get_auth_store] = lambda: AuthStore(tmp_path / "auth.json")
    app.dependency_overrides[get_web_session_store] = lambda: WebSessionStore(
        tmp_path / "web_sessions.json"
    )
    app.dependency_overrides[get_audit_log] = lambda: audit_log
    app.dependency_overrides[get_review_store] = lambda: review_store
    app.dependency_overrides[get_metadata_store] = lambda: metadata_store
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
    response_text: str = "Thanks, I've noted that you're here for the appointment.",
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
    body = resp.json()
    assert body["items"] == []
    assert body["total"] == 0
    assert body["has_more"] is False


def test_queue_excludes_entries_with_no_intent(client, audit_log) -> None:
    _login(client)
    audit_log.record(_entry(intent=None))

    resp = client.get("/api/training/queue")

    assert resp.status_code == 200
    assert resp.json()["items"] == []


def test_queue_lists_entries_newest_first_with_pending_review_status(client, audit_log) -> None:
    _login(client)
    audit_log.record(_entry(text="first"))
    audit_log.record(_entry(text="second"))

    resp = client.get("/api/training/queue")

    assert resp.status_code == 200
    body = resp.json()["items"]
    assert [item["entry"]["text"] for item in body] == ["second", "first"]
    assert all(item["review"]["status"] == "pending" for item in body)


def test_queue_reflects_a_saved_review(client, audit_log, review_store) -> None:
    _login(client)
    entry = _entry()
    audit_log.record(entry)
    review_store.set(example_key(entry), TrainingReview(status="approved"))

    resp = client.get("/api/training/queue")

    assert resp.status_code == 200
    assert resp.json()["items"][0]["review"]["status"] == "approved"


def test_queue_filters_by_status(client, audit_log, review_store) -> None:
    _login(client)
    pending = _entry(text="still pending")
    approved = _entry(text="already approved")
    audit_log.record(pending)
    audit_log.record(approved)
    review_store.set(example_key(approved), TrainingReview(status="approved"))

    resp = client.get("/api/training/queue?status=approved")

    assert resp.status_code == 200
    body = resp.json()
    assert [item["entry"]["text"] for item in body["items"]] == ["already approved"]
    assert body["total"] == 1


def test_queue_paginates_with_offset_and_limit(client, audit_log) -> None:
    _login(client)
    for i in range(5):
        audit_log.record(_entry(text=f"entry {i}"))

    first_page = client.get("/api/training/queue?limit=2&offset=0").json()
    second_page = client.get("/api/training/queue?limit=2&offset=2").json()

    assert first_page["total"] == 5
    assert first_page["has_more"] is True
    assert len(first_page["items"]) == 2
    assert second_page["items"][0]["entry"]["text"] != first_page["items"][0]["entry"]["text"]


def test_queue_counts_reflect_the_full_history_not_just_one_page(
    client, audit_log, review_store
) -> None:
    _login(client)
    # More entries than a single page would hold, spanning all three
    # statuses -- this is the regression test for the bug that motivated
    # pagination: counts (and the status-filtered queue) must reflect the
    # full history, not a recent-N window that silently drops older
    # reviewed entries once the dataset outgrows it.
    entries = [_entry(text=f"entry {i}") for i in range(6)]
    for entry in entries:
        audit_log.record(entry)
    review_store.set(example_key(entries[0]), TrainingReview(status="approved"))
    review_store.set(example_key(entries[1]), TrainingReview(status="approved"))
    review_store.set(example_key(entries[2]), TrainingReview(status="rejected"))

    resp = client.get("/api/training/queue/counts")

    assert resp.status_code == 200
    assert resp.json() == {"pending": 3, "approved": 2, "rejected": 1}


def test_queue_counts_requires_authentication(client) -> None:
    resp = client.get("/api/training/queue/counts")
    assert resp.status_code == 401


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
    assert queue["items"][0]["review"]["status"] == "approved"


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


# -- judge/correct script endpoints ----------------------------------------------------------------


def test_judge_training_data_requires_authentication(client) -> None:
    resp = _post(
        client, "/api/training/scripts/judge-training-data", {"judge_models": ["m1", "m2"]}
    )
    assert resp.status_code == 401


def test_judge_training_data_rejects_fewer_than_two_models(client) -> None:
    _login(client)
    resp = _post(client, "/api/training/scripts/judge-training-data", {"judge_models": ["m1"]})
    assert resp.status_code == 400


def test_judge_training_data_runs_and_completes(client) -> None:
    _login(client)
    resp = _post(
        client, "/api/training/scripts/judge-training-data", {"judge_models": ["m1", "m2"]}
    )
    assert resp.status_code == 200
    assert resp.json()["script"] == "judge_training_data"

    final = _wait_until_idle(client)
    assert final["status"] == "completed"

    logs = client.get("/api/training/scripts/logs").json()
    assert any("m1,m2" in line for line in logs["lines"])


def test_correct_training_data_requires_authentication(client) -> None:
    resp = _post(
        client,
        "/api/training/scripts/correct-training-data",
        {"corrector_model": "m1", "judge_models": ["m1", "m2"]},
    )
    assert resp.status_code == 401


def test_correct_training_data_rejects_fewer_than_two_judge_models(client) -> None:
    _login(client)
    resp = _post(
        client,
        "/api/training/scripts/correct-training-data",
        {"corrector_model": "m1", "judge_models": ["m1"]},
    )
    assert resp.status_code == 400


def test_correct_training_data_runs_and_completes(client) -> None:
    _login(client)
    resp = _post(
        client,
        "/api/training/scripts/correct-training-data",
        {"corrector_model": "m1", "judge_models": ["m1", "m2"]},
    )
    assert resp.status_code == 200
    assert resp.json()["script"] == "correct_training_data"

    final = _wait_until_idle(client)
    assert final["status"] == "completed"


# -- queue metadata ----------------------------------------------------------------


def test_queue_includes_judge_metadata_when_present(client, audit_log, metadata_store) -> None:
    _login(client)
    entry = _entry()
    audit_log.record(entry)
    metadata_store.set(
        example_key(entry),
        TrainingMetadata(
            judge=AggregatedJudgeResult(
                visitor_voice_avg=9.0,
                category_correct_avg=9.0,
                safety_compliant_avg=9.0,
                natural_quality_avg=9.0,
                voice_veto=False,
                safety_veto=False,
                disagreement=0.0,
                per_judge=[],
            )
        ),
    )

    resp = client.get("/api/training/queue")
    assert resp.status_code == 200
    item = resp.json()["items"][0]
    assert item["metadata"]["judge"]["category_correct_avg"] == 9.0


def test_queue_defaults_to_empty_metadata_when_unjudged(client, audit_log) -> None:
    _login(client)
    audit_log.record(_entry())

    resp = client.get("/api/training/queue")
    item = resp.json()["items"][0]
    assert item["metadata"]["judge"] is None
    assert item["metadata"]["corrections"] == []


# -- DPO export ----------------------------------------------------------------


def test_export_dpo_requires_authentication(client) -> None:
    resp = client.get("/api/training/export/dpo")
    assert resp.status_code == 401


def test_export_dpo_is_empty_before_anything_is_judged(client, audit_log) -> None:
    _login(client)
    audit_log.record(_entry())
    resp = client.get("/api/training/export/dpo")
    assert resp.status_code == 200
    assert resp.text == ""
