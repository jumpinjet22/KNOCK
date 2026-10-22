"""WebAuthn/passkey registration and sign-in.

No hand-rolled crypto or COSE/attestation parsing -- `py_webauthn` (the
Python port of the SimpleWebAuthn project) does the actual ceremony
verification; this module is just the KNOCK-specific plumbing around it:
credential storage (`PasskeyStore`), resolving the relying-party id/origin
from the live request, and carrying the per-ceremony challenge between the
options and verify steps of each ceremony (see `_ChallengeStore`).

Registration is authenticated (only an already-logged-in admin can attach a
new passkey to their own account). Login is not, by definition -- a passkey
*is* how you'd sign in without a password. Single-operator model: there is
normally exactly one `AuthStore` user, so the login-options endpoint offers
every passkey registered to any user as `allow_credentials` without needing
a username up front.

WebAuthn itself requires a secure context -- HTTPS, or exactly `localhost`/
`127.0.0.1` -- not a bare LAN IP. The frontend is responsible for checking
`window.isSecureContext` and explaining this rather than letting the
browser API fail with an opaque error; nothing here can relax that
requirement, it's enforced by the browser itself.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any

import webauthn
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from webauthn.helpers.exceptions import InvalidAuthenticationResponse, InvalidRegistrationResponse
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from knock.api.auth_routes import CurrentUserDep, WebSessionStoreDep, _issue_session
from knock.core.auth import PasskeyCredential, PasskeyStore

router = APIRouter(prefix="/api/webauthn", tags=["webauthn"])

RP_NAME = "KNOCK"
_CHALLENGE_SESSION_KEY = "webauthn_challenge"


class _ChallengeStore:
    """Server-side record of issued challenges: each verifies at most once.

    The Starlette session is a signed cookie the browser holds, so popping a
    challenge from it only changes the cookie sent back -- replaying the
    pre-verify cookie alongside a captured response would present the same
    challenge again. Synced passkeys (iCloud Keychain, Google Password
    Manager) always report a sign count of 0, so the sign-count check can't
    catch that replay either; consuming the challenge here does.

    In-process memory: KNOCK runs a single uvicorn worker, and a ceremony
    interrupted by a restart just has to be started again.
    """

    def __init__(
        self,
        ttl_seconds: float = 300.0,
        max_pending: int = 1000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._max_pending = max_pending
        self._clock = clock
        self._expires: dict[str, float] = {}
        self._lock = threading.Lock()

    def issue(self, challenge_b64url: str) -> None:
        with self._lock:
            now = self._clock()
            self._expires = {c: exp for c, exp in self._expires.items() if exp > now}
            # The login-options endpoint is unauthenticated -- bound memory by
            # dropping the oldest pending challenges first.
            while len(self._expires) >= self._max_pending:
                del self._expires[next(iter(self._expires))]
            self._expires[challenge_b64url] = now + self._ttl

    def consume(self, challenge_b64url: str) -> bool:
        with self._lock:
            expires = self._expires.pop(challenge_b64url, None)
            return expires is not None and expires > self._clock()


_challenges = _ChallengeStore()


def _start_ceremony(request: Request, challenge: bytes) -> None:
    challenge_b64url = webauthn.helpers.bytes_to_base64url(challenge)
    _challenges.issue(challenge_b64url)
    request.session[_CHALLENGE_SESSION_KEY] = challenge_b64url


def _finish_ceremony(request: Request, ceremony: str) -> bytes:
    challenge_b64url = request.session.pop(_CHALLENGE_SESSION_KEY, None)
    if not challenge_b64url:
        raise HTTPException(status_code=400, detail=f"no {ceremony} ceremony in progress")
    if not _challenges.consume(challenge_b64url):
        raise HTTPException(
            status_code=400, detail=f"{ceremony} challenge expired or already used; start again"
        )
    return webauthn.base64url_to_bytes(challenge_b64url)


def get_passkey_store() -> PasskeyStore:
    return PasskeyStore()


PasskeyStoreDep = Annotated[PasskeyStore, Depends(get_passkey_store)]


def _rp_id(request: Request) -> str:
    host = request.url.hostname
    if not host:
        raise HTTPException(status_code=400, detail="could not determine relying party id")
    return host


def _origin(request: Request) -> str:
    return f"{request.url.scheme}://{request.url.netloc}"


def _descriptor(credential_id_b64url: str) -> PublicKeyCredentialDescriptor:
    return PublicKeyCredentialDescriptor(id=webauthn.base64url_to_bytes(credential_id_b64url))


# -- registration ---------------------------------------------------------------------


@router.post("/register/options")
def get_registration_options(
    request: Request, current_user: CurrentUserDep, *, store: PasskeyStoreDep
) -> Response:
    existing = store.list_for_user(current_user)
    options = webauthn.generate_registration_options(
        rp_id=_rp_id(request),
        rp_name=RP_NAME,
        user_name=current_user,
        exclude_credentials=[_descriptor(cred.credential_id) for cred in existing],
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.PREFERRED,
        ),
    )
    _start_ceremony(request, options.challenge)
    return Response(content=webauthn.options_to_json(options), media_type="application/json")


class VerifyRegistrationRequest(BaseModel):
    credential: dict[str, Any]
    nickname: str = ""


class PasskeyOut(BaseModel):
    credential_id: str
    nickname: str
    created_at: datetime


@router.post("/register/verify", response_model=PasskeyOut)
def verify_registration(
    body: VerifyRegistrationRequest,
    request: Request,
    current_user: CurrentUserDep,
    *,
    store: PasskeyStoreDep,
) -> PasskeyOut:
    expected_challenge = _finish_ceremony(request, "registration")

    try:
        verification = webauthn.verify_registration_response(
            credential=body.credential,
            expected_challenge=expected_challenge,
            expected_rp_id=_rp_id(request),
            expected_origin=_origin(request),
        )
    except InvalidRegistrationResponse as exc:
        raise HTTPException(status_code=400, detail=f"passkey registration failed: {exc}") from exc

    credential_id = webauthn.helpers.bytes_to_base64url(verification.credential_id)
    credential = PasskeyCredential(
        credential_id=credential_id,
        public_key=webauthn.helpers.bytes_to_base64url(verification.credential_public_key),
        sign_count=verification.sign_count,
        username=current_user,
        nickname=body.nickname,
        created_at=datetime.now(UTC),
    )
    store.add(credential)
    return PasskeyOut(
        credential_id=credential.credential_id,
        nickname=credential.nickname,
        created_at=credential.created_at,
    )


@router.get("", response_model=list[PasskeyOut])
def list_passkeys(current_user: CurrentUserDep, *, store: PasskeyStoreDep) -> list[PasskeyOut]:
    return [
        PasskeyOut(
            credential_id=cred.credential_id, nickname=cred.nickname, created_at=cred.created_at
        )
        for cred in store.list_for_user(current_user)
    ]


@router.delete("/{credential_id}", status_code=204)
def delete_passkey(
    credential_id: str, current_user: CurrentUserDep, *, store: PasskeyStoreDep
) -> None:
    existing = store.get(credential_id)
    if existing is not None and existing.username == current_user:
        store.delete(credential_id)


# -- login ------------------------------------------------------------------------


@router.post("/login/options")
def get_authentication_options(request: Request, *, store: PasskeyStoreDep) -> Response:
    all_credentials = store.list_all()
    if not all_credentials:
        raise HTTPException(status_code=400, detail="no passkeys are registered")

    options = webauthn.generate_authentication_options(
        rp_id=_rp_id(request),
        allow_credentials=[_descriptor(cred.credential_id) for cred in all_credentials],
        user_verification=UserVerificationRequirement.PREFERRED,
    )
    _start_ceremony(request, options.challenge)
    return Response(content=webauthn.options_to_json(options), media_type="application/json")


class VerifyAuthenticationRequest(BaseModel):
    credential: dict[str, Any]


class MeResponse(BaseModel):
    username: str


@router.post("/login/verify", response_model=MeResponse)
def verify_authentication(
    body: VerifyAuthenticationRequest,
    request: Request,
    response: Response,
    *,
    store: PasskeyStoreDep,
    session_store: WebSessionStoreDep,
) -> MeResponse:
    expected_challenge = _finish_ceremony(request, "sign-in")

    raw_credential_id = body.credential.get("id")
    stored = store.get(raw_credential_id) if raw_credential_id else None
    if stored is None:
        raise HTTPException(status_code=401, detail="unknown passkey")

    try:
        verification = webauthn.verify_authentication_response(
            credential=body.credential,
            expected_challenge=expected_challenge,
            expected_rp_id=_rp_id(request),
            expected_origin=_origin(request),
            credential_public_key=webauthn.base64url_to_bytes(stored.public_key),
            credential_current_sign_count=stored.sign_count,
        )
    except InvalidAuthenticationResponse as exc:
        raise HTTPException(status_code=401, detail=f"passkey sign-in failed: {exc}") from exc

    # A new sign count that doesn't advance past what's on file is the
    # classic cloned-authenticator signal (CTAP2 counters are meant to be
    # strictly increasing) -- refuse rather than silently accept.
    if verification.new_sign_count <= stored.sign_count and stored.sign_count != 0:
        raise HTTPException(status_code=401, detail="passkey sign count did not advance")
    store.update_sign_count(stored.credential_id, verification.new_sign_count)

    _issue_session(request, response, session_store, stored.username)
    return MeResponse(username=stored.username)
