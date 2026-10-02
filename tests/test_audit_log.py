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
