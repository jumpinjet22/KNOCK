from datetime import UTC, datetime

import pytest
import webauthn as webauthn_lib
from fastapi.testclient import TestClient
from webauthn.authentication.verify_authentication_response import VerifiedAuthentication
from webauthn.helpers.exceptions import InvalidAuthenticationResponse, InvalidRegistrationResponse
from webauthn.helpers.structs import (
    AttestationFormat,
    CredentialDeviceType,
    PublicKeyCredentialType,
)
from webauthn.registration.verify_registration_response import VerifiedRegistration

from knock.api import webauthn_routes
from knock.api.app import app
from knock.api.auth_routes import get_auth_store, get_web_session_store
from knock.api.webauthn_routes import get_passkey_store
from knock.core.auth import AuthStore, PasskeyCredential, PasskeyStore, WebSessionStore


@pytest.fixture
def passkey_store(tmp_path) -> PasskeyStore:
    return PasskeyStore(tmp_path / "passkeys.json")


@pytest.fixture
def client(tmp_path, passkey_store):
    app.dependency_overrides[get_auth_store] = lambda: AuthStore(tmp_path / "auth.json")
    app.dependency_overrides[get_web_session_store] = lambda: WebSessionStore(
        tmp_path / "web_sessions.json"
    )
    app.dependency_overrides[get_passkey_store] = lambda: passkey_store
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _post(client: TestClient, url: str, json_body: dict | None = None):
    headers = {}
    token = client.cookies.get("csrftoken")
    if token:
        headers["x-csrftoken"] = token
    return client.post(url, json=json_body, headers=headers)


def _login(client: TestClient) -> None:
    resp = _post(client, "/api/auth/setup", {"username": "jon", "password": "a-good-password"})
    assert resp.status_code == 201


def _fake_verified_registration(credential_id: bytes = b"cred-1") -> VerifiedRegistration:
    return VerifiedRegistration(
        credential_id=credential_id,
        credential_public_key=b"fake-public-key",
        sign_count=0,
        aaguid="",
        fmt=AttestationFormat.NONE,
        credential_type=PublicKeyCredentialType.PUBLIC_KEY,
        user_verified=True,
        attestation_object=b"",
        credential_device_type=CredentialDeviceType.SINGLE_DEVICE,
        credential_backed_up=False,
    )


def _fake_verified_authentication(new_sign_count: int = 1) -> VerifiedAuthentication:
    return VerifiedAuthentication(
        credential_id=b"cred-1",
        new_sign_count=new_sign_count,
        credential_device_type=CredentialDeviceType.SINGLE_DEVICE,
        credential_backed_up=False,
        user_verified=True,
    )


# -- registration options ------------------------------------------------------------


def test_registration_options_requires_authentication(client) -> None:
    resp = _post(client, "/api/webauthn/register/options")
    assert resp.status_code == 401


def test_registration_options_returns_real_webauthn_options(client) -> None:
    _login(client)
    resp = _post(client, "/api/webauthn/register/options")

    assert resp.status_code == 200
    body = resp.json()
    assert "challenge" in body
    assert body["rp"]["name"] == "KNOCK"
    assert body["user"]["name"] == "jon"


# -- registration verify --------------------------------------------------------------


def test_registration_verify_rejects_without_prior_options_call(client) -> None:
    _login(client)
    resp = _post(
        client, "/api/webauthn/register/verify", {"credential": {"id": "abc"}, "nickname": ""}
    )
    assert resp.status_code == 400


def test_registration_verify_stores_a_new_passkey(client, passkey_store, monkeypatch) -> None:
    _login(client)
    _post(client, "/api/webauthn/register/options")

    monkeypatch.setattr(
        webauthn_routes.webauthn,
        "verify_registration_response",
        lambda **kwargs: _fake_verified_registration(),
    )

    resp = _post(
        client,
        "/api/webauthn/register/verify",
        {"credential": {"id": "whatever"}, "nickname": "YubiKey"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["nickname"] == "YubiKey"

    stored = passkey_store.list_for_user("jon")
    assert len(stored) == 1
    assert stored[0].nickname == "YubiKey"


def test_registration_verify_surfaces_library_rejection_as_400(client, monkeypatch) -> None:
    _login(client)
    _post(client, "/api/webauthn/register/options")

    def _raise(**kwargs):
        raise InvalidRegistrationResponse("bad signature")

    monkeypatch.setattr(webauthn_routes.webauthn, "verify_registration_response", _raise)

    resp = _post(client, "/api/webauthn/register/verify", {"credential": {"id": "x"}})
    assert resp.status_code == 400


# -- list / delete --------------------------------------------------------------------


def test_list_passkeys_requires_authentication(client) -> None:
    resp = client.get("/api/webauthn")
    assert resp.status_code == 401


def test_list_passkeys_returns_only_the_current_users_credentials(client, passkey_store) -> None:
    _login(client)

    passkey_store.add(
        PasskeyCredential(
            credential_id="cred-1",
            public_key="pub",
            sign_count=0,
            username="jon",
            nickname="Laptop",
            created_at=datetime.now(UTC),
        )
    )
    passkey_store.add(
        PasskeyCredential(
            credential_id="cred-2",
            public_key="pub2",
            sign_count=0,
            username="someone-else",
            nickname="Not mine",
            created_at=datetime.now(UTC),
        )
    )

    resp = client.get("/api/webauthn")

    assert resp.status_code == 200
    assert [p["credential_id"] for p in resp.json()] == ["cred-1"]


def test_delete_passkey_requires_authentication(client) -> None:
    resp = client.delete("/api/webauthn/cred-1")
    assert resp.status_code == 401


def test_delete_passkey_removes_own_credential(client, passkey_store) -> None:
    _login(client)

    passkey_store.add(
        PasskeyCredential(
            credential_id="cred-1",
            public_key="pub",
            sign_count=0,
            username="jon",
            created_at=datetime.now(UTC),
        )
    )
    headers = {}
    token = client.cookies.get("csrftoken")
    if token:
        headers["x-csrftoken"] = token

    resp = client.delete("/api/webauthn/cred-1", headers=headers)

    assert resp.status_code == 204
    assert passkey_store.get("cred-1") is None


def test_delete_passkey_cannot_remove_someone_elses_credential(client, passkey_store) -> None:
    _login(client)

    passkey_store.add(
        PasskeyCredential(
            credential_id="cred-1",
            public_key="pub",
            sign_count=0,
            username="someone-else",
            created_at=datetime.now(UTC),
        )
    )
    headers = {}
    token = client.cookies.get("csrftoken")
    if token:
        headers["x-csrftoken"] = token

    resp = client.delete("/api/webauthn/cred-1", headers=headers)

    assert resp.status_code == 204
    assert passkey_store.get("cred-1") is not None


# -- login options/verify --------------------------------------------------------------


def test_login_options_requires_at_least_one_registered_passkey(client) -> None:
    resp = client.post("/api/webauthn/login/options")
    assert resp.status_code == 400


def test_login_options_returns_real_webauthn_options(client, passkey_store) -> None:

    passkey_store.add(
        PasskeyCredential(
            credential_id=webauthn_lib.helpers.bytes_to_base64url(b"cred-1"),
            public_key="pub",
            sign_count=0,
            username="jon",
            created_at=datetime.now(UTC),
        )
    )

    resp = client.post("/api/webauthn/login/options")

    assert resp.status_code == 200
    body = resp.json()
    assert "challenge" in body
    assert len(body["allowCredentials"]) == 1


def test_login_verify_rejects_without_prior_options_call(client) -> None:
    resp = _post(client, "/api/webauthn/login/verify", {"credential": {"id": "cred-1"}})
    assert resp.status_code == 400


def test_login_verify_issues_a_session_for_a_known_credential(
    client, passkey_store, monkeypatch
) -> None:

    cred_id = webauthn_lib.helpers.bytes_to_base64url(b"cred-1")
    passkey_store.add(
        PasskeyCredential(
            credential_id=cred_id,
            public_key=webauthn_lib.helpers.bytes_to_base64url(b"fake-public-key"),
            sign_count=0,
            username="jon",
            created_at=datetime.now(UTC),
        )
    )
    client.post("/api/webauthn/login/options")

    monkeypatch.setattr(
        webauthn_routes.webauthn,
        "verify_authentication_response",
        lambda **kwargs: _fake_verified_authentication(new_sign_count=1),
    )

    resp = _post(client, "/api/webauthn/login/verify", {"credential": {"id": cred_id}})

    assert resp.status_code == 200
    assert resp.json() == {"username": "jon"}
    assert "knock_session" in resp.cookies

    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json() == {"username": "jon"}


def test_login_verify_rejects_an_unknown_credential_id(client, passkey_store) -> None:
    registered_id = webauthn_lib.helpers.bytes_to_base64url(b"cred-1")
    passkey_store.add(
        PasskeyCredential(
            credential_id=registered_id,
            public_key=webauthn_lib.helpers.bytes_to_base64url(b"fake-public-key"),
            sign_count=0,
            username="jon",
            created_at=datetime.now(UTC),
        )
    )
    client.post("/api/webauthn/login/options")

    resp = _post(client, "/api/webauthn/login/verify", {"credential": {"id": "does-not-exist"}})

    assert resp.status_code == 401


def test_login_verify_rejects_a_non_advancing_sign_count(
    client, passkey_store, monkeypatch
) -> None:

    cred_id = webauthn_lib.helpers.bytes_to_base64url(b"cred-1")
    passkey_store.add(
        PasskeyCredential(
            credential_id=cred_id,
            public_key=webauthn_lib.helpers.bytes_to_base64url(b"fake-public-key"),
            sign_count=5,
            username="jon",
            created_at=datetime.now(UTC),
        )
    )
    client.post("/api/webauthn/login/options")

    monkeypatch.setattr(
        webauthn_routes.webauthn,
        "verify_authentication_response",
        lambda **kwargs: _fake_verified_authentication(new_sign_count=5),
    )

    resp = _post(client, "/api/webauthn/login/verify", {"credential": {"id": cred_id}})

    assert resp.status_code == 401


def test_login_verify_surfaces_library_rejection_as_401(client, passkey_store, monkeypatch) -> None:

    cred_id = webauthn_lib.helpers.bytes_to_base64url(b"cred-1")
    passkey_store.add(
        PasskeyCredential(
            credential_id=cred_id,
            public_key=webauthn_lib.helpers.bytes_to_base64url(b"fake-public-key"),
            sign_count=0,
            username="jon",
            created_at=datetime.now(UTC),
        )
    )
    client.post("/api/webauthn/login/options")

    def _raise(**kwargs):
        raise InvalidAuthenticationResponse("bad signature")

    monkeypatch.setattr(webauthn_routes.webauthn, "verify_authentication_response", _raise)

    resp = _post(client, "/api/webauthn/login/verify", {"credential": {"id": cred_id}})
    assert resp.status_code == 401
