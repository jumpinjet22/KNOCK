"""Training-data export for the Phase 9 LoRA distillation workflow.

Turns reviewed, approved audit-log entries into an instruction-tuning
JSONL file using the exact same prompts `Orchestrator` already sends to
the LLM in production (`_classification_prompt`/`_response_prompt`) -- so
a small fine-tuned model trains on literally the same input shape it'll
see at inference time, not some different format invented just for
training.

Deliberately not a schema change to `AuditEntry`/`audit.jsonl` itself (see
orchestrator.py's own backward-compatibility notes on that file): review
state -- approved/rejected, plus an optional corrected intent or response
text -- lives in a small separate JSON store keyed by a deterministic
hash of each entry's own content, so it works for every already-recorded
entry without needing a stable id to have existed when it was written.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from knock.conversation.policy import _contains_unsafe_disclosure
from knock.core.audit import AuditEntry, JSONLAuditLog
from knock.core.orchestrator import (
    _LLM_CLASSIFIABLE_INTENTS,
    _classification_prompt,
    _response_prompt,
)
from knock.core.training_correction import CorrectionAttempt
from knock.core.training_judge import (
    AggregatedJudgeResult,
    DpoPair,
    build_classification_dpo_pairs,
    build_dpo_pairs,
)

DEFAULT_TRAINING_REVIEWS_ENV_VAR = "KNOCK_TRAINING_REVIEWS"
DEFAULT_TRAINING_METADATA_ENV_VAR = "KNOCK_TRAINING_METADATA"
TRAINING_MODE_ENV_VAR = "KNOCK_TRAINING_MODE"

ReviewStatus = Literal["pending", "approved", "rejected"]


def training_mode_enabled() -> bool:
    """Whether this deployment should collapse the web UI to just the
    Training page (`web/src/lib/uiMode.tsx`) and skip login entirely
    (`require_auth` in `knock.api.auth_routes`) -- a single-purpose local
    deployment, e.g. a scratch machine only ever used to review synthetic
    training data. Set via `KNOCK_TRAINING_MODE=1` in the environment.
    """
    truthy = {"1", "true", "yes", "on"}
    return os.environ.get(TRAINING_MODE_ENV_VAR, "").strip().lower() in truthy


# The classification prompt only ever offers these labels (plus
# "unknown") as valid choices -- see `_classification_prompt`. Used to
# keep a stray intent value (e.g. "occupancy_probe", which can reach
# classify_intent() without ever going through the LLM safety net) from
# producing a classification example whose "correct answer" isn't even
# among the choices its own instruction offers.
VALID_TRAINING_INTENTS = frozenset({*_LLM_CLASSIFIABLE_INTENTS, "unknown"})


def _default_training_reviews_path() -> Path:
    configured = os.environ.get(DEFAULT_TRAINING_REVIEWS_ENV_VAR)
    if configured:
        return Path(configured)
    return Path.home() / ".local" / "share" / "knock" / "training_reviews.json"


def _default_training_metadata_path() -> Path:
    configured = os.environ.get(DEFAULT_TRAINING_METADATA_ENV_VAR)
    if configured:
        return Path(configured)
    return Path.home() / ".local" / "share" / "knock" / "training_metadata.json"


def example_key(entry: AuditEntry) -> str:
    """A deterministic id for one audit entry, derived from its own
    content rather than stored on the entry itself -- works for every
    already-recorded entry, including ones written before this feature
    existed, without needing a migration.
    """
    raw = f"{entry.timestamp.isoformat()}|{entry.text}|{entry.response_text}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


class TrainingReview(BaseModel):
    """One human decision on one audit entry: include it in the exported
    training set as-is, with a correction, or not at all.
    """

    status: ReviewStatus = "pending"
    intent_override: str | None = None
    response_override: str | None = None
    comment: str | None = None


class TrainingReviewStore:
    """One small JSON file: `{"<example_key>": {"status": ..., ...}}`."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else _default_training_reviews_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _read(self) -> dict[str, object]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, data: dict[str, object]) -> None:
        tmp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        tmp_path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp_path, self.path)  # atomic on POSIX

    def set(self, key: str, review: TrainingReview) -> None:
        data = self._read()
        data[key] = review.model_dump(exclude_none=True)
        self._write(data)

    def all(self) -> dict[str, TrainingReview]:
        return {key: TrainingReview.model_validate(value) for key, value in self._read().items()}


class TrainingMetadata(BaseModel):
    """Judge-ensemble and correction-history data for one audit entry --
    kept separate from `TrainingReview` (a human decision) since this is
    machine-generated context *for* that decision, not the decision
    itself. One file, not split further: judge and correction data have a
    natural parent-child relationship (a correction only exists because of
    a judge verdict), so splitting them would buy nothing.
    """

    judge: AggregatedJudgeResult | None = None
    corrections: list[CorrectionAttempt] = []


class TrainingMetadataStore:
    """One small JSON file: `{"<example_key>": {"judge": ..., "corrections": [...]}}`,
    same atomic-write pattern as `TrainingReviewStore`.
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else _default_training_metadata_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _read(self) -> dict[str, object]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, data: dict[str, object]) -> None:
        tmp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        tmp_path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp_path, self.path)  # atomic on POSIX

    def set(self, key: str, metadata: TrainingMetadata) -> None:
        data = self._read()
        data[key] = metadata.model_dump()
        self._write(data)

    def all(self) -> dict[str, TrainingMetadata]:
        return {key: TrainingMetadata.model_validate(value) for key, value in self._read().items()}


class TrainingRecord(BaseModel):
    """One instruction/output pair, ready to write as one JSONL line."""

    task: Literal["classification", "phrasing"]
    instruction: str
    output: str


def build_training_records(entry: AuditEntry, review: TrainingReview) -> list[TrainingRecord]:
    """The one or two training records one approved entry produces.

    Entries with no classified intent (emergency/blocked requests, which
    never reach `classify_intent()`) have nothing to train on here -- the
    same exclusion `AuditLog.count_by_intent()` already applies, since
    their response came from the deterministic `PolicyEngine`/`RESPONSES`
    path, not an LLM prompt.
    """
    if entry.intent is None:
        return []

    intent = review.intent_override or entry.intent
    response_text = review.response_override or entry.response_text

    records: list[TrainingRecord] = []
    if intent in VALID_TRAINING_INTENTS:
        records.append(
            TrainingRecord(
                task="classification",
                instruction=_classification_prompt(entry.text),
                output=intent,
            )
        )
    # A human approving an entry overwhelmingly means "the category's
    # right," not "I re-read this exact phrasing for safety" -- found live
    # via a fine-tuning run that kept reproducing "I'll let <name> know
    # you're here" on person_lookup/official_visit cases: 19 approved
    # examples contained that exact leak verbatim (18 of them approved
    # with no edit at all). Training directly on text the production
    # backstop would itself suppress teaches a model to reproduce the one
    # failure mode this whole project exists to prevent -- so the same
    # `apply_style()` check gates what's allowed into the phrasing half of
    # the export, same as it gates a live response before it ever reaches
    # a visitor. The classification record above is unaffected -- the
    # category label on all 19 was correct, only the phrasing was bad.
    if response_text and not _contains_unsafe_disclosure(response_text):
        records.append(
            TrainingRecord(
                task="phrasing",
                instruction=_response_prompt(intent, entry.text),
                output=response_text,
            )
        )
    return records


def export_training_jsonl(
    audit_log: JSONLAuditLog, review_store: TrainingReviewStore, *, limit: int = 10000
) -> str:
    """Every approved entry's training records, one JSON object per line."""
    reviews = review_store.all()
    lines: list[str] = []
    for entry in audit_log.recent(limit=limit):
        review = reviews.get(example_key(entry))
        if review is None or review.status != "approved":
            continue
        lines.extend(record.model_dump_json() for record in build_training_records(entry, review))
    return "".join(f"{line}\n" for line in lines)


def export_dpo_pairs_jsonl(
    audit_log: JSONLAuditLog,
    review_store: TrainingReviewStore,
    metadata_store: TrainingMetadataStore,
    *,
    limit: int = 10000,
    margin_threshold: float = 2.0,
) -> str:
    """Every DPO preference pair derivable from judged entries, one JSON
    object per line -- plain `{"prompt", "chosen", "rejected"}` triples
    (trl's standard DPO format).

    The "chosen" side of every pair must come from a human-approved entry
    (the same `TrainingReview.status == "approved"` gate the SFT export
    already uses) -- the "rejected" side needs no explicit rejection of
    its own, since losing the comparison is enough. This mirrors the SFT
    export's human-in-the-loop stance rather than trusting the judge
    ensemble alone to certify training data, consistent with this
    project's own finding that automated review missed real problems a
    careful human pass caught.

    Only entries with a recorded judge verdict (`TrainingMetadata.judge`)
    participate -- an un-judged entry simply contributes no pairs, it's
    not treated as a failure.
    """
    reviews = review_store.all()
    metadata = metadata_store.all()

    judged: list[tuple[AuditEntry, AggregatedJudgeResult]] = []
    approved_keys: set[str] = set()
    for entry in audit_log.recent(limit=limit):
        key = example_key(entry)
        review = reviews.get(key)
        if review is not None and review.status == "approved":
            approved_keys.add(key)
        meta = metadata.get(key)
        if meta is not None and meta.judge is not None:
            judged.append((entry, meta.judge))

    def is_approved(entry: AuditEntry) -> bool:
        return example_key(entry) in approved_keys

    by_scenario: dict[str, list[tuple[AuditEntry, AggregatedJudgeResult]]] = {}
    for entry, result in judged:
        by_scenario.setdefault(entry.text, []).append((entry, result))

    pairs: list[DpoPair] = []
    for scenario_candidates in by_scenario.values():
        pairs.extend(
            build_classification_dpo_pairs(
                scenario_candidates, margin_threshold=margin_threshold, chosen_allowed=is_approved
            )
        )
        by_intent: dict[str, list[tuple[AuditEntry, AggregatedJudgeResult]]] = {}
        for entry, result in scenario_candidates:
            if entry.intent is not None:
                by_intent.setdefault(entry.intent, []).append((entry, result))
        for intent_candidates in by_intent.values():
            pairs.extend(
                build_dpo_pairs(
                    intent_candidates, margin_threshold=margin_threshold, chosen_allowed=is_approved
                )
            )

    lines = [pair.model_dump_json() for pair in pairs]
    return "".join(f"{line}\n" for line in lines)
