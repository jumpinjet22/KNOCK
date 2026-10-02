import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from knock.api.app import app
from knock.api.auth_routes import get_auth_store, get_web_session_store
from knock.api.settings_routes import get_config_store
from knock.api.video_routes import SnapshotCache, get_snapshot_cache, get_unifi_client_factory
from knock.config import FrigateConfig
from knock.core.auth import AuthStore, WebSessionStore
from knock.core.config_store import ConfigStore


class _FakeCamera:
    def __init__(self, camera_id: str, name: str, is_reachable: bool = True) -> None:
        self.id = camera_id
        self.name = name
        self.is_reachable = is_reachable


class _FakeProtectClient:
    """Stands in for `ProtectApiClient`'s *public* API surface -- the only
    surface available to a client constructed with just an `api_key` (no
    username/password), which is all `UnifiConfig` has.
    """

    def __init__(
        self,
        cameras: list[_FakeCamera] | None = None,
        snapshots: dict[str, bytes | None] | None = None,
        raise_on_list: bool = False,
    ) -> None:
        self.cameras = cameras or []
        self.snapshots = snapshots or {}
        self.raise_on_list = raise_on_list
        self.closed = False
        self.snapshot_calls = 0

    async def get_cameras_public(self) -> list[_FakeCamera]:
        if self.raise_on_list:
            raise ConnectionError("no route to host")
        return self.cameras

    async def get_public_api_camera_snapshot(self, device_id: str) -> bytes | None:
        self.snapshot_calls += 1
        return self.snapshots.get(device_id)

    async def close_session(self) -> None:
        self.closed = True


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
    # A fixed instance, not a factory lambda -- FastAPI calls the override
    # on every request, so a `lambda: SnapshotCache(...)` would hand out a
    # fresh, empty cache each time and the floor would never actually kick in.
    cache = SnapshotCache(min_interval=0.0)
    app.dependency_overrides[get_snapshot_cache] = lambda: cache
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


def _with_fake_unifi_client(fake: _FakeProtectClient):
    app.dependency_overrides[get_unifi_client_factory] = lambda: lambda config: fake


# -- auth gating ----------------------------------------------------------------


def test_list_unifi_cameras_requires_authentication(client) -> None:
    resp = client.get("/api/video/unifi/cameras")
    assert resp.status_code == 401


def test_unifi_snapshot_requires_authentication(client) -> None:
    resp = client.get("/api/video/unifi/cam1/snapshot")
    assert resp.status_code == 401


def test_list_frigate_cameras_requires_authentication(client) -> None:
    resp = client.get("/api/video/frigate/cameras")
    assert resp.status_code == 401


def test_frigate_snapshot_requires_authentication(client) -> None:
    resp = client.get("/api/video/frigate/front_door/snapshot")
    assert resp.status_code == 401


# -- unifi ------------------------------------------------------------------------


def test_list_unifi_cameras_returns_camera_info(client) -> None:
    _login(client)
    fake = _FakeProtectClient(cameras=[_FakeCamera("abc123", "Front Door", True)])
    _with_fake_unifi_client(fake)

    resp = client.get("/api/video/unifi/cameras")

    assert resp.status_code == 200
    assert resp.json() == [{"device_id": "abc123", "name": "Front Door", "is_connected": True}]
    assert fake.closed is True


def test_list_unifi_cameras_reports_connection_failure_as_502(client) -> None:
    _login(client)
    fake = _FakeProtectClient(raise_on_list=True)
    _with_fake_unifi_client(fake)

    resp = client.get("/api/video/unifi/cameras")

    assert resp.status_code == 502


def test_get_unifi_snapshot_returns_jpeg_bytes(client) -> None:
    _login(client)
    fake = _FakeProtectClient(snapshots={"abc123": b"\xff\xd8fake-jpeg"})
    _with_fake_unifi_client(fake)

    resp = client.get("/api/video/unifi/abc123/snapshot")

    assert resp.status_code == 200
    assert resp.content == b"\xff\xd8fake-jpeg"
    assert resp.headers["content-type"] == "image/jpeg"


def test_get_unifi_snapshot_reports_missing_snapshot_as_502(client) -> None:
    _login(client)
    fake = _FakeProtectClient(snapshots={})
    _with_fake_unifi_client(fake)

    resp = client.get("/api/video/unifi/abc123/snapshot")

    assert resp.status_code == 502


def test_unifi_snapshot_cache_avoids_hammering_within_the_floor(tmp_path, config_store) -> None:
    app.dependency_overrides[get_auth_store] = lambda: AuthStore(tmp_path / "auth.json")
    app.dependency_overrides[get_web_session_store] = lambda: WebSessionStore(
        tmp_path / "web_sessions.json"
    )
    app.dependency_overrides[get_config_store] = lambda: config_store
    cache = SnapshotCache(min_interval=60.0)
    app.dependency_overrides[get_snapshot_cache] = lambda: cache
    fake = _FakeProtectClient(snapshots={"abc123": b"\xff\xd8one"})
    _with_fake_unifi_client(fake)
    try:
        client = TestClient(app)
        _login(client)

        client.get("/api/video/unifi/abc123/snapshot")
        client.get("/api/video/unifi/abc123/snapshot")
        client.get("/api/video/unifi/abc123/snapshot")

        assert fake.snapshot_calls == 1
    finally:
        app.dependency_overrides.clear()


# -- frigate ----------------------------------------------------------------------


@respx.mock
def test_list_frigate_cameras_returns_names_from_frigate_config(client, config_store) -> None:
    _login(client)
    config = FrigateConfig.from_sources(config_store)
    respx.get(f"http://{config.http_host}:{config.http_port}/api/config").mock(
        return_value=httpx.Response(200, json={"cameras": {"front_door": {}, "backyard": {}}})
    )

    resp = client.get("/api/video/frigate/cameras")

    assert resp.status_code == 200
    assert {item["name"] for item in resp.json()} == {"front_door", "backyard"}


@respx.mock
def test_list_frigate_cameras_reports_connection_failure_as_502(client, config_store) -> None:
    _login(client)
    config = FrigateConfig.from_sources(config_store)
    respx.get(f"http://{config.http_host}:{config.http_port}/api/config").mock(
        side_effect=httpx.ConnectError("refused")
    )

    resp = client.get("/api/video/frigate/cameras")

    assert resp.status_code == 502


@respx.mock
def test_get_frigate_snapshot_returns_jpeg_bytes(client, config_store) -> None:
    _login(client)
    config = FrigateConfig.from_sources(config_store)
    respx.get(f"http://{config.http_host}:{config.http_port}/api/front_door/latest.jpg").mock(
        return_value=httpx.Response(200, content=b"\xff\xd8frigate-jpeg")
    )

    resp = client.get("/api/video/frigate/front_door/snapshot")

    assert resp.status_code == 200
    assert resp.content == b"\xff\xd8frigate-jpeg"
    assert resp.headers["content-type"] == "image/jpeg"


@respx.mock
def test_get_frigate_snapshot_reports_upstream_failure_as_502(client, config_store) -> None:
    _login(client)
    config = FrigateConfig.from_sources(config_store)
    respx.get(f"http://{config.http_host}:{config.http_port}/api/front_door/latest.jpg").mock(
        return_value=httpx.Response(404)
    )

    resp = client.get("/api/video/frigate/front_door/snapshot")

    assert resp.status_code == 502
