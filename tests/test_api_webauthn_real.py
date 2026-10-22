"""Passkey registration and sign-in through the real `webauthn` verification.

test_api_webauthn.py patches `verify_registration_response` /
`verify_authentication_response` out, so nothing there checks that KNOCK hands
the library the right challenge, origin, RP id and stored public key. These
drive the real endpoints with `_SoftAuthenticator`, a software authenticator
that produces genuinely signed responses, and nothing patched.
"""

import base64
import hashlib
import json
import os
from typing import Any

import cbor2
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from knock.api.app import app
from knock.api.auth_routes import get_auth_store, get_web_session_store
from knock.api.webauthn_routes import get_passkey_store
from knock.core.auth import AuthStore, PasskeyStore, WebSessionStore

ORIGIN = "http://testserver"

# Authenticator data flags (WebAuthn §6.1).
_USER_PRESENT, _USER_VERIFIED, _ATTESTED_CREDENTIAL = 0x01, 0x04, 0x40


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class _SoftAuthenticator:
    """A minimal ES256 authenticator ("none" attestation), like a passkey.

    Like a real one, it's bound to the RP id it registered with and always
    signs for that, whatever RP id a later sign-in asks for -- it's the
    browser, not the authenticator, that reports the origin.
    `sign_count_step=0` mimics synced passkeys (iCloud Keychain, Google
    Password Manager), which always report a sign count of 0.
    """

    def __init__(self, sign_count_step: int = 1) -> None:
        self._key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = os.urandom(16)
        self.sign_count = 0
        self._step = sign_count_step
        self._rp_id = ""

    def _cose_public_key(self) -> bytes:
        numbers = self._key.public_key().public_numbers()
        return cbor2.dumps(
            {
                1: 2,  # kty: EC2
                3: -7,  # alg: ES256
                -1: 1,  # crv: P-256
                -2: numbers.x.to_bytes(32, "big"),
                -3: numbers.y.to_bytes(32, "big"),
            }
        )

    @staticmethod
    def _client_data(ceremony: str, challenge: str, origin: str) -> bytes:
        return json.dumps(
            {"type": ceremony, "challenge": challenge, "origin": origin, "crossOrigin": False}
        ).encode()

    def register(self, options: dict[str, Any], origin: str = ORIGIN) -> dict[str, Any]:
        self._rp_id = options["rp"]["id"]
        auth_data = (
            hashlib.sha256(self._rp_id.encode()).digest()
            + bytes([_USER_PRESENT | _USER_VERIFIED | _ATTESTED_CREDENTIAL])
            + self.sign_count.to_bytes(4, "big")
            + bytes(16)  # AAGUID
            + len(self.credential_id).to_bytes(2, "big")
            + self.credential_id
            + self._cose_public_key()
        )
        attestation = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        client_data = self._client_data("webauthn.create", options["challenge"], origin)
        return {
            "id": _b64url(self.credential_id),
            "rawId": _b64url(self.credential_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": _b64url(client_data),
                "attestationObject": _b64url(attestation),
            },
        }

    def sign_in(self, options: dict[str, Any], origin: str = ORIGIN) -> dict[str, Any]:
        self.sign_count += self._step
        auth_data = (
            hashlib.sha256(self._rp_id.encode()).digest()
            + bytes([_USER_PRESENT | _USER_VERIFIED])
            + self.sign_count.to_bytes(4, "big")
        )
        client_data = self._client_data("webauthn.get", options["challenge"], origin)
        signature = self._key.sign(
            auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256())
        )
        return {
            "id": _b64url(self.credential_id),
            "rawId": _b64url(self.credential_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": _b64url(client_data),
                "authenticatorData": _b64url(auth_data),
                "signature": _b64url(signature),
            },
        }


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


def _register(client: TestClient, authenticator: _SoftAuthenticator) -> None:
    resp = _post(client, "/api/auth/setup", {"username": "jon", "password": "a-good-password"})
    assert resp.status_code == 201
    options = _post(client, "/api/webauthn/register/options").json()
    resp = _post(
        client,
        "/api/webauthn/register/verify",
        {"credential": authenticator.register(options), "nickname": "soft"},
    )
    assert resp.status_code == 200, resp.text
    client.cookies.clear()


def _sign_in(client: TestClient, authenticator: _SoftAuthenticator, **kwargs):
    options = _post(client, "/api/webauthn/login/options").json()
    return _post(
        client,
        "/api/webauthn/login/verify",
        {"credential": authenticator.sign_in(options, **kwargs)},
    )


def test_real_passkey_registers_and_signs_in(client, passkey_store) -> None:
    authenticator = _SoftAuthenticator()
    _register(client, authenticator)

    resp = _sign_in(client, authenticator)

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"username": "jon"}
    assert client.get("/api/auth/me").json() == {"username": "jon"}
    [stored] = passkey_store.list_for_user("jon")
    assert stored.sign_count == 1


def test_signing_in_twice_advances_the_sign_count(client, passkey_store) -> None:
    authenticator = _SoftAuthenticator()
    _register(client, authenticator)

    assert _sign_in(client, authenticator).status_code == 200
    assert _sign_in(client, authenticator).status_code == 200

    [stored] = passkey_store.list_for_user("jon")
    assert stored.sign_count == 2


def test_a_tampered_signature_is_rejected(client) -> None:
    authenticator = _SoftAuthenticator()
    _register(client, authenticator)
    options = _post(client, "/api/webauthn/login/options").json()
    credential = authenticator.sign_in(options)
    signature = bytearray(base64.urlsafe_b64decode(credential["response"]["signature"] + "=="))
    signature[-1] ^= 0x01
    credential["response"]["signature"] = _b64url(bytes(signature))

    resp = _post(client, "/api/webauthn/login/verify", {"credential": credential})

    assert resp.status_code == 401
    assert client.get("/api/auth/me").status_code == 401


def test_a_response_signed_for_another_challenge_is_rejected(client) -> None:
    authenticator = _SoftAuthenticator()
    _register(client, authenticator)
    _post(client, "/api/webauthn/login/options")
    stale = {"challenge": _b64url(os.urandom(32))}

    resp = _post(client, "/api/webauthn/login/verify", {"credential": authenticator.sign_in(stale)})

    assert resp.status_code == 401


def test_a_response_from_another_origin_is_rejected(client) -> None:
    authenticator = _SoftAuthenticator()
    _register(client, authenticator)

    resp = _sign_in(client, authenticator, origin="https://evil.example")

    assert resp.status_code == 401


def test_a_passkey_only_works_on_the_hostname_it_was_registered_on(client, passkey_store) -> None:
    # The RP id and origin come from the request's own hostname, so a passkey
    # registered at one address (e.g. knock.local) is refused at another
    # (e.g. the raw IP) -- pinned here so that stays deliberate.
    authenticator = _SoftAuthenticator()
    _register(client, authenticator)
    other = TestClient(app, base_url="http://otherhost")

    resp = _sign_in(other, authenticator, origin="http://otherhost")

    assert resp.status_code == 401


def test_a_sign_count_that_goes_backwards_is_rejected(client) -> None:
    authenticator = _SoftAuthenticator()
    _register(client, authenticator)
    assert _sign_in(client, authenticator).status_code == 200
    authenticator.sign_count = -1  # next sign-in reports 0: a cloned authenticator

    resp = _sign_in(client, authenticator)

    assert resp.status_code == 401
