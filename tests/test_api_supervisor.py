import sys
import time

import pytest
from fastapi.testclient import TestClient

from knock.api.app import app
from knock.api.auth_routes import get_auth_store, get_web_session_store
from knock.api.settings_routes import get_config_store
from knock.api.supervisor_routes import get_bridge_supervisor
from knock.core.auth import AuthStore, WebSessionStore
from knock.core.config_store import ConfigStore
from knock.core.supervisor import BridgeSupervisor


def _sleepy_command(seconds: float = 30.0) -> list[str]:
    return [sys.executable, "-c", f"import time; time.sleep({seconds})"]


def _wait_until(predicate, *, timeout: float = 5.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


@pytest.fixture
def supervisor(tmp_path) -> BridgeSupervisor:
    store = ConfigStore(tmp_path / "config.json")
    return BridgeSupervisor(
        store,
        commands={
            "mqtt": _sleepy_command(),
            "frigate": _sleepy_command(),
            "homeassistant": _sleepy_command(),
            "unifi": _sleepy_command(),
        },
    )


@pytest.fixture
def client(tmp_path, supervisor):
    app.dependency_overrides[get_auth_store] = lambda: AuthStore(tmp_path / "auth.json")
    app.dependency_overrides[get_web_session_store] = lambda: WebSessionStore(
        tmp_path / "web_sessions.json"
    )
    app.dependency_overrides[get_config_store] = lambda: ConfigStore(tmp_path / "config.json")
    app.dependency_overrides[get_bridge_supervisor] = lambda: supervisor
    try:
        yield TestClient(app)
    finally:
        supervisor.shutdown_all()
        app.dependency_overrides.clear()


def _post(client: TestClient, url: str):
    headers = {}
    token = client.cookies.get("csrftoken")
    if token:
        headers["x-csrftoken"] = token
    return client.post(url, headers=headers)


def _put(client: TestClient, url: str, json: dict):
    headers = {}
    token = client.cookies.get("csrftoken")
    if token:
        headers["x-csrftoken"] = token
    return client.put(url, json=json, headers=headers)


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


# -- auth gating ----------------------------------------------------------------


def test_list_bridges_requires_authentication(client) -> None:
    resp = client.get("/api/supervisor")
    assert resp.status_code == 401


def test_start_bridge_requires_authentication(client) -> None:
    resp = _post(client, "/api/supervisor/mqtt/start")
    assert resp.status_code == 401


# -- listing / 404 ----------------------------------------------------------------


def test_list_bridges_returns_all_four_stopped_initially(client) -> None:
    _login(client)
    resp = client.get("/api/supervisor")
    assert resp.status_code == 200
    body = resp.json()
    assert {item["name"] for item in body} == {"mqtt", "frigate", "homeassistant", "unifi"}
    assert all(item["status"] == "stopped" for item in body)
    assert all(item["autostart"] is False for item in body)


def test_unknown_bridge_name_is_404(client) -> None:
    _login(client)
    resp = client.get("/api/supervisor/not-a-bridge")
    assert resp.status_code == 404


# -- start / stop / restart ----------------------------------------------------------


def test_start_then_stop_bridge(client, supervisor) -> None:
    _login(client)
    resp = _post(client, "/api/supervisor/mqtt/start")
    assert resp.status_code == 200

    assert _wait_until(lambda: supervisor.describe("mqtt").status == "running")
    resp = client.get("/api/supervisor/mqtt")
    assert resp.json()["status"] == "running"
    assert resp.json()["pid"] is not None

    resp = _post(client, "/api/supervisor/mqtt/stop")
    assert resp.status_code == 200
    assert _wait_until(lambda: supervisor.describe("mqtt").status == "stopped")


def test_restart_bridge_changes_pid(client, supervisor) -> None:
    _login(client)
    _post(client, "/api/supervisor/mqtt/start")
    assert _wait_until(lambda: supervisor.describe("mqtt").status == "running")
    pid_before = supervisor.describe("mqtt").pid

    resp = _post(client, "/api/supervisor/mqtt/restart")
    assert resp.status_code == 200
    assert _wait_until(lambda: supervisor.describe("mqtt").status == "running")
    assert supervisor.describe("mqtt").pid != pid_before


# -- autostart --------------------------------------------------------------------


def test_set_autostart_requires_authentication(client) -> None:
    resp = client.put("/api/supervisor/mqtt/autostart", json={"enabled": True})
    assert resp.status_code == 401


def test_set_autostart_updates_and_persists(client, supervisor) -> None:
    _login(client)
    resp = _put(client, "/api/supervisor/mqtt/autostart", {"enabled": True})
    assert resp.status_code == 200
    assert resp.json()["autostart"] is True
    assert supervisor.get_autostart("mqtt") is True

    resp = client.get("/api/supervisor/mqtt")
    assert resp.json()["autostart"] is True


def test_set_autostart_on_an_unknown_bridge_is_404(client) -> None:
    _login(client)
    resp = _put(client, "/api/supervisor/not-a-bridge/autostart", {"enabled": True})
    assert resp.status_code == 404


# -- logs -----------------------------------------------------------------------------


def test_get_bridge_logs_returns_empty_before_start(client) -> None:
    _login(client)
    resp = client.get("/api/supervisor/mqtt/logs")
    assert resp.status_code == 200
    assert resp.json() == {"lines": [], "next_after": 0}
