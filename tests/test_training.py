import json
from datetime import UTC, datetime

from knock.core.audit import AuditEntry, JSONLAuditLog
from knock.core.training import (
    TrainingMetadata,
    TrainingMetadataStore,
    TrainingReview,
    TrainingReviewStore,
    build_training_records,
    example_key,
    export_dpo_pairs_jsonl,
    export_training_jsonl,
)
from knock.core.training_judge import AggregatedJudgeResult, JudgeAxisScores


def _entry(
    text: str = "I'm here to work on your AC unit",
    response_text: str = "Thanks, I've noted that you're here for the appointment.",
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
    assert phrasing.output == "Thanks, I've noted that you're here for the appointment."


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


def test_build_training_records_skips_phrasing_for_an_approved_but_unsafe_response() -> None:
    # Found live: a human approving an entry overwhelmingly means "the
    # category's right," not "I re-read this exact phrasing for safety" --
    # 19 approved training examples turned out to contain "I'll let <name>
    # know you're here" verbatim, 18 of them approved with no edit at all.
    # Training directly on text the production backstop would itself
    # suppress just teaches a model to reproduce that exact leak. The
    # classification record is unaffected -- only the phrasing is unsafe
    # here, not the category.
    entry = _entry(
        text="Is John here?",
        response_text="I'll let John know you stopped by.",
        intent="person_lookup",
    )
    records = build_training_records(entry, TrainingReview(status="approved"))

    assert {r.task for r in records} == {"classification"}
    assert records[0].output == "person_lookup"


def test_build_training_records_skips_phrasing_for_an_unsafe_response_override() -> None:
    # The filter applies to the *final* response text (after any human
    # correction), not just the original -- an override can still be
    # unsafe (one of the 19 live cases was an edited response that still
    # left the leak in place).
    entry = _entry(intent="official_visit")
    review = TrainingReview(
        status="approved", response_override="I'll let the household know you stopped by."
    )
    records = build_training_records(entry, review)

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


# -- TrainingMetadataStore ----------------------------------------------------------------


def _ok_judge_result(
    category_correct: float = 9.0, natural_quality: float = 9.0
) -> AggregatedJudgeResult:
    return AggregatedJudgeResult(
        visitor_voice_avg=9.0,
        category_correct_avg=category_correct,
        safety_compliant_avg=9.0,
        natural_quality_avg=natural_quality,
        voice_veto=False,
        safety_veto=False,
        disagreement=0.0,
        per_judge=[
            JudgeAxisScores(
                visitor_voice=9,
                category_correct=int(category_correct),
                safety_compliant=9,
                natural_quality=int(natural_quality),
                reason="looks good",
                judge_model="judge-a",
            )
        ],
    )


def test_metadata_store_round_trips(tmp_path) -> None:
    store = TrainingMetadataStore(tmp_path / "metadata.json")
    metadata = TrainingMetadata(judge=_ok_judge_result())
    store.set("abc123", metadata)

    reloaded = TrainingMetadataStore(tmp_path / "metadata.json")
    result = reloaded.all()["abc123"]
    assert result.judge is not None
    assert result.judge.category_correct_avg == 9.0
    assert result.corrections == []


def test_metadata_store_returns_empty_before_anything_is_saved(tmp_path) -> None:
    store = TrainingMetadataStore(tmp_path / "metadata.json")
    assert store.all() == {}


# -- export_dpo_pairs_jsonl ----------------------------------------------------------------


def test_export_dpo_pairs_requires_chosen_to_be_approved(tmp_path) -> None:
    audit_log = JSONLAuditLog(tmp_path / "audit.jsonl")
    review_store = TrainingReviewStore(tmp_path / "reviews.json")
    metadata_store = TrainingMetadataStore(tmp_path / "metadata.json")

    good_unapproved = _entry(text="scenario", response_text="Great reply")
    worse_approved = _entry(text="scenario", response_text="Worse reply")
    audit_log.record(good_unapproved)
    audit_log.record(worse_approved)

    review_store.set(example_key(worse_approved), TrainingReview(status="approved"))
    metadata_store.set(
        example_key(good_unapproved), TrainingMetadata(judge=_ok_judge_result(10, 10))
    )
    metadata_store.set(example_key(worse_approved), TrainingMetadata(judge=_ok_judge_result(2, 2)))

    jsonl = export_dpo_pairs_jsonl(audit_log, review_store, metadata_store, margin_threshold=2.0)
    # good_unapproved scored higher but was never approved -- it can't be
    # "chosen", and worse_approved scored too low to win on its own, so no
    # valid pair exists.
    assert jsonl == ""


def test_export_dpo_pairs_creates_pair_when_chosen_is_approved(tmp_path) -> None:
    audit_log = JSONLAuditLog(tmp_path / "audit.jsonl")
    review_store = TrainingReviewStore(tmp_path / "reviews.json")
    metadata_store = TrainingMetadataStore(tmp_path / "metadata.json")

    good_approved = _entry(text="scenario", response_text="Great reply")
    bad_unreviewed = _entry(text="scenario", response_text="Bad reply")
    audit_log.record(good_approved)
    audit_log.record(bad_unreviewed)

    review_store.set(example_key(good_approved), TrainingReview(status="approved"))
    metadata_store.set(example_key(good_approved), TrainingMetadata(judge=_ok_judge_result(10, 10)))
    metadata_store.set(example_key(bad_unreviewed), TrainingMetadata(judge=_ok_judge_result(2, 2)))

    jsonl = export_dpo_pairs_jsonl(audit_log, review_store, metadata_store, margin_threshold=2.0)
    lines = [line for line in jsonl.strip().splitlines() if line]
    assert len(lines) == 1
    pair = json.loads(lines[0])
    assert pair["chosen"] == "Great reply"
    assert pair["rejected"] == "Bad reply"


def test_export_dpo_pairs_skips_entries_with_no_judge_metadata(tmp_path) -> None:
    audit_log = JSONLAuditLog(tmp_path / "audit.jsonl")
    review_store = TrainingReviewStore(tmp_path / "reviews.json")
    metadata_store = TrainingMetadataStore(tmp_path / "metadata.json")

    entry = _entry()
    audit_log.record(entry)
    review_store.set(example_key(entry), TrainingReview(status="approved"))
    # No metadata_store.set() call -- this entry was never judged.

    assert export_dpo_pairs_jsonl(audit_log, review_store, metadata_store) == ""
