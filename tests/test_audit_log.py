import json
from datetime import UTC, datetime

from knock.core.audit import AuditEntry, JSONLAuditLog, NullAuditLog


def _entry(text: str = "Hi I have a package", intent: str | None = "delivery") -> AuditEntry:
    return AuditEntry(
        timestamp=datetime.now(UTC),
        text=text,
        matched_flags={},
        matched_rule_ids=[],
        allowed=True,
        reason="normal",
        intent=intent,
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


def test_count_by_intent_tallies_each_intent(tmp_path) -> None:
    log = JSONLAuditLog(tmp_path / "audit.jsonl")
    log.record(_entry("one", intent="delivery"))
    log.record(_entry("two", intent="soliciting"))
    log.record(_entry("three", intent="soliciting"))
    log.record(_entry("four", intent="religious_soliciting"))

    counts = log.count_by_intent()

    assert counts == {"delivery": 1, "soliciting": 2, "religious_soliciting": 1}


def test_count_by_intent_excludes_entries_with_no_intent(tmp_path) -> None:
    log = JSONLAuditLog(tmp_path / "audit.jsonl")
    log.record(_entry("emergency case", intent=None))
    log.record(_entry("blocked case", intent=None))

    assert log.count_by_intent() == {}


def test_count_by_intent_returns_empty_dict_when_file_does_not_exist(tmp_path) -> None:
    log = JSONLAuditLog(tmp_path / "does-not-exist.jsonl")
    assert log.count_by_intent() == {}
