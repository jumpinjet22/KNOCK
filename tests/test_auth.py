import stat
from datetime import UTC, datetime, timedelta

from knock.core.auth import AuthStore, WebSession, WebSessionStore

# -- AuthStore ------------------------------------------------------------------


def test_has_any_user_is_false_initially(tmp_path) -> None:
    store = AuthStore(tmp_path / "auth.json")
    assert store.has_any_user() is False


def test_create_user_then_has_any_user_is_true(tmp_path) -> None:
    store = AuthStore(tmp_path / "auth.json")
    store.create_user("jon", "correct-horse-battery-staple")
    assert store.has_any_user() is True


def test_create_user_rejects_duplicate_username(tmp_path) -> None:
    store = AuthStore(tmp_path / "auth.json")
    store.create_user("jon", "password1")
    try:
        store.create_user("jon", "password2")
        raised = False
    except ValueError:
        raised = True
    assert raised is True


def test_verify_password_accepts_correct_password(tmp_path) -> None:
    store = AuthStore(tmp_path / "auth.json")
    store.create_user("jon", "correct-horse-battery-staple")
    assert store.verify_password("jon", "correct-horse-battery-staple") is True


def test_verify_password_rejects_wrong_password(tmp_path) -> None:
    store = AuthStore(tmp_path / "auth.json")
    store.create_user("jon", "correct-horse-battery-staple")
    assert store.verify_password("jon", "wrong-password") is False


def test_verify_password_rejects_unknown_user(tmp_path) -> None:
    store = AuthStore(tmp_path / "auth.json")
    assert store.verify_password("nobody", "anything") is False


def test_password_hash_is_not_the_plaintext_password(tmp_path) -> None:
    store = AuthStore(tmp_path / "auth.json")
    user = store.create_user("jon", "correct-horse-battery-staple")
    assert "correct-horse-battery-staple" not in user.password_hash
    assert user.password_hash.startswith("$argon2id$")


def test_set_password_changes_which_password_verifies(tmp_path) -> None:
    store = AuthStore(tmp_path / "auth.json")
    store.create_user("jon", "old-password")
    store.set_password("jon", "new-password")

    assert store.verify_password("jon", "old-password") is False
    assert store.verify_password("jon", "new-password") is True


def test_auth_store_persists_across_instances(tmp_path) -> None:
    path = tmp_path / "auth.json"
    AuthStore(path).create_user("jon", "a-password")

    reloaded = AuthStore(path)
    assert reloaded.has_any_user() is True
    assert reloaded.verify_password("jon", "a-password") is True


def test_auth_file_has_owner_only_permissions(tmp_path) -> None:
    path = tmp_path / "auth.json"
    AuthStore(path).create_user("jon", "a-password")

    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == (stat.S_IRUSR | stat.S_IWUSR)


def test_auth_store_default_path_honors_env_var(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("KNOCK_AUTH_PATH", str(tmp_path / "custom-auth.json"))
    assert AuthStore().path == tmp_path / "custom-auth.json"


# -- WebSessionStore --------------------------------------------------------------


def test_create_returns_a_session_bound_to_the_username(tmp_path) -> None:
    store = WebSessionStore(tmp_path / "sessions.json")
    session = store.create("jon")

    assert session.username == "jon"
    assert len(session.token) > 20


def test_get_returns_the_created_session(tmp_path) -> None:
    store = WebSessionStore(tmp_path / "sessions.json")
    created = store.create("jon")

    fetched = store.get(created.token)

    assert fetched is not None
    assert fetched.username == "jon"
    assert fetched.token == created.token


def test_get_returns_none_for_unknown_token(tmp_path) -> None:
    store = WebSessionStore(tmp_path / "sessions.json")
    assert store.get("not-a-real-token") is None


def test_delete_invalidates_the_session(tmp_path) -> None:
    store = WebSessionStore(tmp_path / "sessions.json")
    session = store.create("jon")

    store.delete(session.token)

    assert store.get(session.token) is None


def test_get_returns_none_and_deletes_an_expired_session(tmp_path) -> None:
    path = tmp_path / "sessions.json"
    store = WebSessionStore(path)
    expired = WebSession(
        token="expired-token",
        username="jon",
        created_at=datetime.now(UTC) - timedelta(days=60),
        expires_at=datetime.now(UTC) - timedelta(days=1),
    )
    store._save({expired.token: expired})

    assert store.get("expired-token") is None
    # confirm it was actually pruned from disk, not just filtered on read
    assert WebSessionStore(path)._load() == {}


def test_two_sessions_for_the_same_user_are_independent(tmp_path) -> None:
    store = WebSessionStore(tmp_path / "sessions.json")
    first = store.create("jon")
    second = store.create("jon")

    store.delete(first.token)

    assert store.get(first.token) is None
    assert store.get(second.token) is not None


def test_web_session_store_persists_across_instances(tmp_path) -> None:
    path = tmp_path / "sessions.json"
    session = WebSessionStore(path).create("jon")

    reloaded = WebSessionStore(path)
    assert reloaded.get(session.token) is not None


def test_web_session_file_has_owner_only_permissions(tmp_path) -> None:
    path = tmp_path / "sessions.json"
    WebSessionStore(path).create("jon")

    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == (stat.S_IRUSR | stat.S_IWUSR)
