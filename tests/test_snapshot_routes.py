import pytest
from fastapi.testclient import TestClient

from knock.api.app import app
from knock.api.snapshot_routes import get_snapshot_store
from knock.core.snapshot_store import SnapshotStore


@pytest.fixture
def store(tmp_path) -> SnapshotStore:
    return SnapshotStore(tmp_path)


@pytest.fixture
def client(store):
    app.dependency_overrides[get_snapshot_store] = lambda: store
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def test_get_snapshot_returns_the_image_unauthenticated(client, store) -> None:
    token = store.put(b"jpeg-bytes")

    # No auth header or cookie set at all -- this route must not require one.
    resp = client.get(f"/api/snapshot/{token}")

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/jpeg"
    assert resp.content == b"jpeg-bytes"


def test_get_snapshot_404s_for_an_unknown_token(client) -> None:
    resp = client.get(f"/api/snapshot/{'a' * 32}")
    assert resp.status_code == 404


def test_get_snapshot_is_single_use(client, store) -> None:
    token = store.put(b"jpeg-bytes")

    first = client.get(f"/api/snapshot/{token}")
    second = client.get(f"/api/snapshot/{token}")

    assert first.status_code == 200
    assert second.status_code == 404
