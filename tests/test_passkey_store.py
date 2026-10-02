from datetime import UTC, datetime

import pytest

from knock.core.auth import PasskeyCredential, PasskeyStore


def _credential(credential_id: str = "cred-1", username: str = "jon") -> PasskeyCredential:
    return PasskeyCredential(
        credential_id=credential_id,
        public_key="pub",
        sign_count=0,
        username=username,
        nickname="Laptop",
        created_at=datetime.now(UTC),
    )


def test_add_and_get_round_trips(tmp_path) -> None:
    store = PasskeyStore(tmp_path / "passkeys.json")
    store.add(_credential())

    loaded = store.get("cred-1")
    assert loaded is not None
    assert loaded.username == "jon"
    assert loaded.nickname == "Laptop"


def test_get_missing_credential_returns_none(tmp_path) -> None:
    store = PasskeyStore(tmp_path / "passkeys.json")
    assert store.get("does-not-exist") is None


def test_list_for_user_filters_by_username(tmp_path) -> None:
    store = PasskeyStore(tmp_path / "passkeys.json")
    store.add(_credential("cred-1", "jon"))
    store.add(_credential("cred-2", "someone-else"))

    assert [c.credential_id for c in store.list_for_user("jon")] == ["cred-1"]


def test_list_all_returns_every_credential(tmp_path) -> None:
    store = PasskeyStore(tmp_path / "passkeys.json")
    store.add(_credential("cred-1", "jon"))
    store.add(_credential("cred-2", "someone-else"))

    assert {c.credential_id for c in store.list_all()} == {"cred-1", "cred-2"}


def test_update_sign_count(tmp_path) -> None:
    store = PasskeyStore(tmp_path / "passkeys.json")
    store.add(_credential())

    store.update_sign_count("cred-1", 42)

    updated = store.get("cred-1")
    assert updated is not None
    assert updated.sign_count == 42


def test_update_sign_count_raises_for_unknown_credential(tmp_path) -> None:
    store = PasskeyStore(tmp_path / "passkeys.json")
    with pytest.raises(ValueError):
        store.update_sign_count("does-not-exist", 1)


def test_delete_removes_credential(tmp_path) -> None:
    store = PasskeyStore(tmp_path / "passkeys.json")
    store.add(_credential())

    store.delete("cred-1")

    assert store.get("cred-1") is None


def test_delete_is_a_no_op_for_an_unknown_credential(tmp_path) -> None:
    store = PasskeyStore(tmp_path / "passkeys.json")
    store.delete("does-not-exist")  # should not raise
