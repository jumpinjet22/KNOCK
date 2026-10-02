"""Covers the SPA catch-all in api/app.py: serving the built frontend,
falling back to index.html for client-side routes, and -- critically --
that the path-traversal guard actually holds (see app.py's serve_web()).
"""

import pytest
from fastapi.testclient import TestClient

import knock.api.app as app_module


@pytest.fixture
def dist(tmp_path, monkeypatch):
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    (dist_dir / "index.html").write_text("<html><body>knock-spa</body></html>")
    (dist_dir / "favicon.svg").write_text("<svg>fake-favicon</svg>")
    assets_dir = dist_dir / "assets"
    assets_dir.mkdir()
    (assets_dir / "index.js").write_text("console.log('fake bundle')")

    secret_outside = tmp_path / "secret.txt"
    secret_outside.write_text("do-not-serve-me")

    monkeypatch.setenv("KNOCK_WEB_DIST", str(dist_dir))
    # Re-import with the env var set, since _web_dist is resolved at module
    # import time.
    import importlib

    importlib.reload(app_module)
    try:
        yield dist_dir, secret_outside
    finally:
        monkeypatch.delenv("KNOCK_WEB_DIST", raising=False)
        importlib.reload(app_module)


@pytest.fixture
def client(dist):
    return TestClient(app_module.app)


def test_root_serves_index_html(client) -> None:
    resp = client.get("/")
    assert resp.status_code == 200
    assert "knock-spa" in resp.text


def test_client_side_route_falls_back_to_index_html(client) -> None:
    resp = client.get("/login")
    assert resp.status_code == 200
    assert "knock-spa" in resp.text


def test_real_asset_is_served_directly(client) -> None:
    resp = client.get("/favicon.svg")
    assert resp.status_code == 200
    assert "fake-favicon" in resp.text


def test_nested_asset_is_served_directly(client) -> None:
    resp = client.get("/assets/index.js")
    assert resp.status_code == 200
    assert "fake bundle" in resp.text


def test_existing_api_routes_are_unaffected(client) -> None:
    resp = client.get("/api/auth/status")
    assert resp.status_code == 200
    assert resp.json() == {"setup_required": True}


@pytest.mark.parametrize(
    "path",
    [
        "/../secret.txt",
        "/%2e%2e/secret.txt",
        "/assets/../../secret.txt",
        "/assets/..%2f..%2fsecret.txt",
    ],
)
def test_path_traversal_falls_back_to_index_html_not_the_real_file(client, path) -> None:
    resp = client.get(path)
    assert resp.status_code == 200
    assert "knock-spa" in resp.text
    assert "do-not-serve-me" not in resp.text


def test_no_mount_when_dist_dir_is_missing(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("KNOCK_WEB_DIST", str(tmp_path / "does-not-exist"))
    import importlib

    importlib.reload(app_module)
    try:
        client = TestClient(app_module.app)
        resp = client.get("/some/path")
        assert resp.status_code == 404
    finally:
        monkeypatch.delenv("KNOCK_WEB_DIST", raising=False)
        importlib.reload(app_module)
