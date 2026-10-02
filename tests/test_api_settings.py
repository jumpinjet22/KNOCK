import pytest
from fastapi.testclient import TestClient

from knock.api.app import app
from knock.api.auth_routes import get_auth_store, get_web_session_store
from knock.api.settings_routes import get_config_store
from knock.core.auth import AuthStore, WebSessionStore
from knock.core.config_store import ConfigStore


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
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _post(client: TestClient, url: str, json: dict | None = None):
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


def _field(resp_json: dict, name: str) -> dict:
    return next(f for f in resp_json["fields"] if f["name"] == name)


# -- auth gating ------------------------------------------------------------------


def test_get_settings_requires_authentication(client) -> None:
    resp = client.get("/api/settings/ollama")
    assert resp.status_code == 401


def test_put_settings_requires_authentication(client) -> None:
    resp = _put(client, "/api/settings/ollama", {"values": {"host": "x"}})
    assert resp.status_code == 401


# -- GET: shape + defaults ----------------------------------------------------------


def test_get_unknown_section_is_404(client) -> None:
    _login(client)
    resp = client.get("/api/settings/not-a-real-section")
    assert resp.status_code == 404


def test_get_ollama_settings_reports_defaults(client) -> None:
    _login(client)
    resp = client.get("/api/settings/ollama")

    assert resp.status_code == 200
    body = resp.json()
    assert body["section"] == "ollama"
    assert _field(body, "host")["value"] == "127.0.0.1"
    assert _field(body, "host")["type"] == "string"
    assert _field(body, "port")["type"] == "int"
    assert _field(body, "timeout")["type"] == "float"


def test_vision_prompt_field_is_typed_as_text(client) -> None:
    _login(client)
    resp = client.get("/api/settings/vision")
    assert _field(resp.json(), "prompt")["type"] == "text"


def test_unifi_trigger_on_field_is_typed_as_list_string(client) -> None:
    _login(client)
    resp = client.get("/api/settings/unifi")
    field = _field(resp.json(), "trigger_on")
    assert field["type"] == "list_string"
    assert field["value"] == ["ring"]


def test_homeassistant_verify_ssl_field_is_typed_as_bool(client) -> None:
    _login(client)
    resp = client.get("/api/settings/homeassistant")
    field = _field(resp.json(), "verify_ssl")
    assert field["type"] == "bool"
    assert field["value"] is True


# -- GET: secrets never leak ---------------------------------------------------------


def test_secret_field_has_no_value_when_unset(client) -> None:
    _login(client)
    resp = client.get("/api/settings/unifi")
    field = _field(resp.json(), "api_key")
    assert field["type"] == "secret"
    assert field["value"] is None
    assert field["has_value"] is False


def test_secret_field_reports_has_value_but_never_the_real_value(client, config_store) -> None:
    config_store.set_section("unifi", {"api_key": "super-secret-key"})
    _login(client)

    resp = client.get("/api/settings/unifi")

    field = _field(resp.json(), "api_key")
    assert field["has_value"] is True
    assert field["value"] is None
    assert "super-secret-key" not in resp.text


# -- PUT: updates persist, validate, and respect secret semantics -----------------------


def test_put_updates_a_plain_field(client, config_store) -> None:
    _login(client)
    resp = _put(client, "/api/settings/ollama", {"values": {"host": "192.168.1.54"}})

    assert resp.status_code == 200
    assert _field(resp.json(), "host")["value"] == "192.168.1.54"
    assert config_store.get_section("ollama") == {"host": "192.168.1.54"}


def test_put_rejects_an_invalid_type(client) -> None:
    _login(client)
    resp = _put(client, "/api/settings/ollama", {"values": {"port": "not-a-number"}})
    assert resp.status_code == 422


def test_put_rejects_an_unknown_field(client) -> None:
    _login(client)
    resp = _put(client, "/api/settings/ollama", {"values": {"nope": "x"}})
    assert resp.status_code == 400


def test_put_sets_a_secret_field(client, config_store) -> None:
    _login(client)
    resp = _put(client, "/api/settings/unifi", {"values": {"api_key": "a-real-key"}})

    assert resp.status_code == 200
    field = _field(resp.json(), "api_key")
    assert field["has_value"] is True
    assert field["value"] is None
    assert config_store.get_section("unifi")["api_key"] == "a-real-key"


def test_put_with_blank_secret_leaves_existing_secret_untouched(client, config_store) -> None:
    config_store.set_section("unifi", {"api_key": "original-key"})
    _login(client)

    resp = _put(client, "/api/settings/unifi", {"values": {"host": "192.168.1.1"}})

    assert resp.status_code == 200
    assert config_store.get_section("unifi")["api_key"] == "original-key"
    assert config_store.get_section("unifi")["host"] == "192.168.1.1"


def test_put_only_persists_touched_fields_not_the_full_resolved_set(client, config_store) -> None:
    _login(client)
    _put(client, "/api/settings/ollama", {"values": {"host": "192.168.1.54"}})

    stored = config_store.get_section("ollama")
    assert stored == {"host": "192.168.1.54"}
    assert "model" not in stored
    assert "timeout" not in stored


def test_put_updates_a_list_field(client, config_store) -> None:
    _login(client)
    resp = _put(
        client, "/api/settings/frigate", {"values": {"trigger_labels": ["person", "package"]}}
    )

    assert resp.status_code == 200
    assert _field(resp.json(), "trigger_labels")["value"] == ["person", "package"]
    assert config_store.get_section("frigate")["trigger_labels"] == ["person", "package"]


# -- env-var shadowing ----------------------------------------------------------------


def test_shadowed_by_env_is_true_when_env_var_set(client, monkeypatch) -> None:
    monkeypatch.setenv("KNOCK_OLLAMA_HOST", "10.0.0.1")
    _login(client)

    resp = client.get("/api/settings/ollama")

    field = _field(resp.json(), "host")
    assert field["shadowed_by_env"] is True
    assert field["value"] == "10.0.0.1"


def test_shadowed_by_env_is_false_by_default(client) -> None:
    _login(client)
    resp = client.get("/api/settings/ollama")
    assert _field(resp.json(), "host")["shadowed_by_env"] is False


# -- kokoro voices (best-effort) ------------------------------------------------------


def test_kokoro_voices_reports_error_when_server_unreachable(client) -> None:
    _login(client)
    # Nothing is listening on the default Kokoro port in the test environment.
    resp = client.get("/api/settings/kokoro/voices")

    assert resp.status_code == 200
    body = resp.json()
    assert body["voices"] == []
    assert body["error"] is not None
