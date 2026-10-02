import json
from datetime import UTC, datetime

from knock.core.audit import AuditEntry, JSONLAuditLog, NullAuditLog


def _entry(text: str = "Hi I have a package") -> AuditEntry:
    return AuditEntry(
        timestamp=datetime.now(UTC),
        text=text,
        matched_flags={},
        matched_rule_ids=[],
        allowed=True,
        reason="normal",
        intent="delivery",
    )


def test_jsonl_audit_log_appends_one_json_object_per_line(tmp_path) -> None:
    path = tmp_path / "audit.jsonl"
    log = JSONLAuditLog(path)

    log.record(_entry("first"))
    log.record(_entry("second"))

    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2

    first, second = (json.loads(line) for line in lines)
    assert first["text"] == "first"
    assert second["text"] == "second"
    assert first["allowed"] is True
    assert first["reason"] == "normal"


def test_jsonl_audit_log_creates_parent_directory(tmp_path) -> None:
    path = tmp_path / "nested" / "dir" / "audit.jsonl"
    JSONLAuditLog(path)
    assert path.parent.exists()


def test_null_audit_log_is_a_no_op() -> None:
    # Should not raise, and has nothing observable to assert beyond that.
    NullAuditLog().record(_entry())


def test_recent_returns_newest_first(tmp_path) -> None:
    log = JSONLAuditLog(tmp_path / "audit.jsonl")
    log.record(_entry("first"))
    log.record(_entry("second"))
    log.record(_entry("third"))

    entries = log.recent()

    assert [entry.text for entry in entries] == ["third", "second", "first"]


def test_recent_respects_limit(tmp_path) -> None:
    log = JSONLAuditLog(tmp_path / "audit.jsonl")
    log.record(_entry("first"))
    log.record(_entry("second"))
    log.record(_entry("third"))

    entries = log.recent(limit=2)

    assert [entry.text for entry in entries] == ["third", "second"]


def test_recent_returns_empty_list_when_file_does_not_exist(tmp_path) -> None:
    log = JSONLAuditLog(tmp_path / "does-not-exist.jsonl")
    assert log.recent() == []
