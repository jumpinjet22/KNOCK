import json

import pytest
from fastapi.testclient import TestClient

from knock.api.app import app
from knock.api.auth_routes import get_auth_store, get_web_session_store
from knock.api.settings_routes import get_config_store
from knock.config import DoorsConfig
from knock.core.auth import AuthStore, WebSessionStore
from knock.core.config_store import ConfigStore
from knock.core.supervisor import build_bridge_env


@pytest.fixture
def config_store(tmp_path) -> ConfigStore:
    return ConfigStore(tmp_path / "config.json")


@pytest.fixture(autouse=True)
def _no_ambient_doors(monkeypatch) -> None:
    monkeypatch.delenv("KNOCK_DOORS", raising=False)


@pytest.fixture
def client(tmp_path, config_store):
    app.dependency_overrides[get_auth_store] = lambda: AuthStore(tmp_path / "auth.json")
    app.dependency_overrides[get_web_session_store] = lambda: WebSessionStore(
        tmp_path / "web_sessions.json"
    )
    app.dependency_overrides[get_config_store] = lambda: config_store
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _headers(client: TestClient) -> dict[str, str]:
    token = client.cookies.get("csrftoken")
    return {"x-csrftoken": token} if token else {}


def _login(client: TestClient) -> None:
    resp = client.post(
        "/api/auth/setup",
        json={"username": "jon", "password": "a-good-password"},
        headers=_headers(client),
    )
    assert resp.status_code == 201


def _put(client: TestClient, doors: list[dict]):
    return client.put("/api/doors", json={"doors": doors}, headers=_headers(client))


FRONT = {"key": "front", "name": "Front door", "triggers": {"protect_cameras": ["cam1"]}}


# -- auth ---------------------------------------------------------------------------------------


def test_doors_routes_require_authentication(client) -> None:
    assert client.get("/api/doors").status_code == 401
    assert _put(client, []).status_code == 401


# -- read ---------------------------------------------------------------------------------------


def test_no_doors_configured_is_an_empty_list(client) -> None:
    _login(client)

    body = client.get("/api/doors").json()

    assert body == {"doors": [], "shadowed_by_env": False, "error": None}


def test_saved_doors_come_back_in_full(client) -> None:
    _login(client)
    assert _put(client, [FRONT]).status_code == 200

    [door] = client.get("/api/doors").json()["doors"]

    assert door["key"] == "front"
    assert door["name"] == "Front door"
    assert door["triggers"] == {"protect_cameras": ["cam1"], "ha_entities": []}
    assert door["listen_device"] is None and door["look_devices"] == []


# -- save ---------------------------------------------------------------------------------------


def test_saving_writes_the_doors_section_the_bridge_reads(client, config_store) -> None:
    _login(client)

    assert _put(client, [FRONT]).status_code == 200

    [door] = DoorsConfig.from_sources(config_store).doors
    assert door.key == "front" and door.triggers.protect_cameras == ["cam1"]


def test_saving_an_empty_list_removes_every_door(client, config_store) -> None:
    _login(client)
    _put(client, [FRONT])

    assert _put(client, []).status_code == 200

    assert DoorsConfig.from_sources(config_store).doors == []


@pytest.mark.parametrize(
    ("doors", "message"),
    [
        ([{**FRONT, "key": "../x"}], None),
        ([{"key": "x", "triggers": {}}], "no triggers"),
        ([FRONT, {**FRONT, "triggers": {"protect_cameras": ["cam2"]}}], "used twice"),
        ([FRONT, {**FRONT, "key": "back"}], "triggers both"),
        ([{**FRONT, "speek_device": "x"}], None),
    ],
)
def test_invalid_doors_are_rejected_and_nothing_is_saved(
    client, config_store, doors, message
) -> None:
    _login(client)

    resp = _put(client, doors)

    assert resp.status_code == 422
    if message:
        assert message in json.dumps(resp.json()["detail"])
    assert DoorsConfig.from_sources(config_store).doors == []


def test_an_ha_only_door_must_name_its_devices(client) -> None:
    _login(client)
    door = {"key": "side", "triggers": {"ha_entities": ["binary_sensor.k"]}}

    assert _put(client, [door]).status_code == 422
    full = {**door, "listen_device": "c", "speak_device": "c", "look_devices": ["c"]}
    assert _put(client, [full]).status_code == 200


# -- environment override ---------------------------------------------------------------------


def test_knock_doors_in_the_environment_is_shown_and_blocks_saving(
    client, config_store, monkeypatch
) -> None:
    _login(client)
    monkeypatch.setenv("KNOCK_DOORS", json.dumps([FRONT]))

    body = client.get("/api/doors").json()
    resp = _put(client, [])

    assert body["shadowed_by_env"] is True
    assert [d["key"] for d in body["doors"]] == ["front"]
    assert resp.status_code == 409
    assert "KNOCK_DOORS" in resp.json()["detail"]


def test_an_unreadable_saved_value_is_reported_not_a_crash(client, config_store) -> None:
    _login(client)
    config_store.set_section("doors", {"doors": [{"key": "../x"}]})

    resp = client.get("/api/doors")

    assert resp.status_code == 200
    assert resp.json()["doors"] == [] and resp.json()["error"]


# -- the saved doors reach the bridge -----------------------------------------------------------


def test_saved_doors_reach_the_unifi_bridge_process(client, config_store) -> None:
    _login(client)
    _put(client, [FRONT])

    env = build_bridge_env("unifi", config_store)

    [door] = DoorsConfig(doors=json.loads(env["KNOCK_DOORS"])).doors
    assert door.key == "front"


def test_with_no_saved_doors_the_bridge_environment_is_untouched(config_store) -> None:
    assert "KNOCK_DOORS" not in build_bridge_env("unifi", config_store)


def test_other_bridges_never_get_doors(client, config_store) -> None:
    _login(client)
    _put(client, [FRONT])

    for name in ("mqtt", "frigate", "homeassistant"):
        assert "KNOCK_DOORS" not in build_bridge_env(name, config_store)


def test_an_environment_value_is_passed_through_not_replaced(
    client, config_store, monkeypatch
) -> None:
    _login(client)
    _put(client, [FRONT])
    monkeypatch.setenv("KNOCK_DOORS", "[]")

    assert build_bridge_env("unifi", config_store)["KNOCK_DOORS"] == "[]"


def test_invalid_saved_doors_fall_back_to_no_doors_instead_of_breaking_the_bridge(
    config_store,
) -> None:
    config_store.set_section("doors", {"doors": [{"key": "../x"}]})

    assert "KNOCK_DOORS" not in build_bridge_env("unifi", config_store)
