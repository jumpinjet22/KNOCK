from datetime import UTC, datetime

from knock.core.audit import AuditEntry, JSONLAuditLog
from knock.core.training import (
    TrainingReview,
    TrainingReviewStore,
    build_training_records,
    example_key,
    export_training_jsonl,
)


def _entry(
    text: str = "I'm here to work on your AC unit",
    response_text: str = "Thanks, I'll let them know you're here for the appointment.",
    intent: str | None = "service_appointment",
    timestamp: datetime | None = None,
) -> AuditEntry:
    return AuditEntry(
        timestamp=timestamp or datetime(2026, 1, 1, tzinfo=UTC),
        text=text,
        response_text=response_text,
        matched_flags={},
        matched_rule_ids=[],
        allowed=True,
        reason="normal",
        intent=intent,
    )


# -- example_key ----------------------------------------------------------------


def test_example_key_is_stable_across_separate_computations() -> None:
    entry = _entry()
    assert example_key(entry) == example_key(_entry())


def test_example_key_differs_for_different_content() -> None:
    assert example_key(_entry(text="one")) != example_key(_entry(text="two"))


# -- TrainingReviewStore ----------------------------------------------------------------


def test_review_store_round_trips_a_review(tmp_path) -> None:
    store = TrainingReviewStore(tmp_path / "reviews.json")
    store.set("abc123", TrainingReview(status="approved"))

    assert store.all() == {"abc123": TrainingReview(status="approved")}


def test_review_store_returns_empty_before_anything_is_saved(tmp_path) -> None:
    store = TrainingReviewStore(tmp_path / "reviews.json")
    assert store.all() == {}


def test_review_store_persists_corrections(tmp_path) -> None:
    store = TrainingReviewStore(tmp_path / "reviews.json")
    store.set(
        "abc123",
        TrainingReview(status="approved", intent_override="delivery", response_override="Fixed."),
    )

    reloaded = TrainingReviewStore(tmp_path / "reviews.json")
    review = reloaded.all()["abc123"]
    assert review.intent_override == "delivery"
    assert review.response_override == "Fixed."


# -- build_training_records ----------------------------------------------------------------


def test_build_training_records_produces_both_tasks_for_a_normal_entry() -> None:
    entry = _entry()
    records = build_training_records(entry, TrainingReview(status="approved"))

    tasks = {record.task for record in records}
    assert tasks == {"classification", "phrasing"}

    classification = next(r for r in records if r.task == "classification")
    assert classification.output == "service_appointment"

    phrasing = next(r for r in records if r.task == "phrasing")
    assert phrasing.output == "Thanks, I'll let them know you're here for the appointment."


def test_build_training_records_returns_nothing_for_emergency_or_blocked_entries() -> None:
    entry = _entry(intent=None)
    assert build_training_records(entry, TrainingReview(status="approved")) == []


def test_build_training_records_applies_overrides() -> None:
    entry = _entry(intent="delivery")
    review = TrainingReview(
        status="approved", intent_override="food_delivery", response_override="Corrected reply."
    )
    records = build_training_records(entry, review)

    classification = next(r for r in records if r.task == "classification")
    assert classification.output == "food_delivery"
    phrasing = next(r for r in records if r.task == "phrasing")
    assert phrasing.output == "Corrected reply."


def test_build_training_records_skips_classification_for_an_intent_outside_the_closed_list() -> (
    None
):
    # "occupancy_probe" can reach classify_intent() without ever going
    # through the LLM safety net, so it isn't one of the labels
    # _classification_prompt actually offers -- including it as a
    # classification example would teach the model to output an answer
    # its own instruction doesn't list as a choice.
    entry = _entry(intent="occupancy_probe")
    records = build_training_records(entry, TrainingReview(status="approved"))

    assert {r.task for r in records} == {"phrasing"}


def test_build_training_records_skips_phrasing_when_response_text_is_empty() -> None:
    entry = _entry(response_text="")
    records = build_training_records(entry, TrainingReview(status="approved"))

    assert {r.task for r in records} == {"classification"}


# -- export_training_jsonl ----------------------------------------------------------------


def test_export_includes_only_approved_entries(tmp_path) -> None:
    audit_log = JSONLAuditLog(tmp_path / "audit.jsonl")
    review_store = TrainingReviewStore(tmp_path / "reviews.json")

    approved = _entry(text="approved one")
    pending = _entry(text="still pending")
    rejected = _entry(text="rejected one")
    audit_log.record(approved)
    audit_log.record(pending)
    audit_log.record(rejected)
    review_store.set(example_key(approved), TrainingReview(status="approved"))
    review_store.set(example_key(rejected), TrainingReview(status="rejected"))

    jsonl = export_training_jsonl(audit_log, review_store)

    lines = [line for line in jsonl.strip().splitlines() if line]
    assert len(lines) == 2  # the approved entry's classification + phrasing records
    assert "approved one" in jsonl
    assert "still pending" not in jsonl
    assert "rejected one" not in jsonl


def test_export_is_empty_when_nothing_is_approved(tmp_path) -> None:
    audit_log = JSONLAuditLog(tmp_path / "audit.jsonl")
    review_store = TrainingReviewStore(tmp_path / "reviews.json")
    audit_log.record(_entry())

    assert export_training_jsonl(audit_log, review_store) == ""
