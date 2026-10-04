from datetime import timedelta

from knock.core.snapshot_store import SnapshotStore


def test_put_then_take_returns_the_same_bytes(tmp_path) -> None:
    store = SnapshotStore(tmp_path)
    token = store.put(b"jpeg-bytes")

    assert store.take(token) == b"jpeg-bytes"


def test_take_is_single_use(tmp_path) -> None:
    store = SnapshotStore(tmp_path)
    token = store.put(b"jpeg-bytes")

    store.take(token)

    assert store.take(token) is None


def test_take_returns_none_for_an_unknown_token(tmp_path) -> None:
    store = SnapshotStore(tmp_path)
    assert store.take("a" * 32) is None


def test_take_returns_none_for_a_malformed_token(tmp_path) -> None:
    store = SnapshotStore(tmp_path)
    assert store.take("../../etc/passwd") is None
    assert store.take("") is None


def test_take_returns_none_past_ttl(tmp_path) -> None:
    store = SnapshotStore(tmp_path, ttl=timedelta(seconds=0))
    token = store.put(b"jpeg-bytes")

    assert store.take(token) is None


def test_put_prunes_expired_entries(tmp_path) -> None:
    store = SnapshotStore(tmp_path, ttl=timedelta(seconds=0))
    stale_token = store.put(b"old")
    stale_path = tmp_path / f"{stale_token}.jpg"
    assert stale_path.exists()

    store.put(b"new")  # a fresh put() should prune the already-expired stale one

    assert not stale_path.exists()
