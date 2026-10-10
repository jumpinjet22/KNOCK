"""Training-data review & export routes -- the human-in-the-loop step
before Phase 9's LoRA distillation workflow: look at real interactions,
approve or correct each one, then export an instruction-tuning JSONL file
built from exactly the same prompts `Orchestrator` sends to the LLM in
production. Sits behind `require_auth` for the same reason the audit log
itself does -- this can contain raw visitor speech.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from knock.api.auth_routes import CurrentUserDep
from knock.config import OllamaConfig
from knock.core.audit import AuditEntry, JSONLAuditLog
from knock.core.script_runner import RunState, ScriptName, ScriptRunner
from knock.core.training import (
    VALID_TRAINING_INTENTS,
    TrainingMetadata,
    TrainingMetadataStore,
    TrainingReview,
    TrainingReviewStore,
    example_key,
    export_dpo_pairs_jsonl,
    export_training_jsonl,
    training_mode_enabled,
)
from knock.core.training_correction import CorrectionAttempt, correct_response
from knock.core.training_judge import aggregate_scores, score_candidate
from knock.providers.llm.ollama import OllamaProvider

router = APIRouter(prefix="/api/training", tags=["training"])

_script_runner = ScriptRunner()


def get_audit_log() -> JSONLAuditLog:
    return JSONLAuditLog()


def get_review_store() -> TrainingReviewStore:
    return TrainingReviewStore()


def get_metadata_store() -> TrainingMetadataStore:
    return TrainingMetadataStore()


def get_script_runner() -> ScriptRunner:
    return _script_runner


AuditLogDep = Annotated[JSONLAuditLog, Depends(get_audit_log)]
ReviewStoreDep = Annotated[TrainingReviewStore, Depends(get_review_store)]
MetadataStoreDep = Annotated[TrainingMetadataStore, Depends(get_metadata_store)]
ScriptRunnerDep = Annotated[ScriptRunner, Depends(get_script_runner)]


class TrainingQueueItem(BaseModel):
    key: str
    entry: AuditEntry
    review: TrainingReview
    metadata: TrainingMetadata = TrainingMetadata()
    needs_attention: bool = False


class TrainingQueuePage(BaseModel):
    items: list[TrainingQueueItem]
    total: int
    offset: int
    limit: int
    has_more: bool


# Mirrors the frontend's own DISAGREEMENT_WARNING_THRESHOLD (Training.tsx) --
# kept as a plain float rather than importing across the API boundary.
_DISAGREEMENT_THRESHOLD = 2.5
_LOW_SCORE_THRESHOLD = 6.0


def _needs_attention(item: TrainingQueueItem) -> bool:
    """A quick, deterministic "does a human actually need to think about
    this one" signal, computed from judge scores already on hand -- not a
    new LLM call, just a threshold over data Stage 3 already produced.

    Unjudged entries default to True (nothing to rubber-stamp confidently
    without scores) -- this only ever matters in practice for very recent
    entries a judging pass hasn't reached yet.
    """
    judge = item.metadata.judge
    if judge is None:
        return True
    return (
        judge.safety_veto
        or judge.disagreement >= _DISAGREEMENT_THRESHOLD
        or judge.category_correct_avg < _LOW_SCORE_THRESHOLD
        or judge.natural_quality_avg < _LOW_SCORE_THRESHOLD
    )


class TrainingQueueCounts(BaseModel):
    pending: int
    approved: int
    rejected: int


def _all_queue_items(
    audit_log: JSONLAuditLog,
    review_store: TrainingReviewStore,
    metadata_store: TrainingMetadataStore,
) -> list[TrainingQueueItem]:
    """Every audit entry with something to train on, newest first, joined
    with its current review status and judge/correction metadata -- the
    full history, not a recent-N window. `recent(limit=100_000)` is this
    project's established "effectively everything" idiom (see
    scripts/judge_training_data.py); a plain linear scan is fine at this
    local-first scale (see JSONLAuditLog.recent's own docstring).

    Callers filter/paginate this list themselves -- this just does the
    join once so `get_training_queue` and `get_training_queue_counts`
    don't each repeat it.
    """
    reviews = review_store.all()
    metadata = metadata_store.all()
    items: list[TrainingQueueItem] = []
    for entry in audit_log.recent(limit=100_000):
        if entry.intent is None:
            continue
        key = example_key(entry)
        item = TrainingQueueItem(
            key=key,
            entry=entry,
            review=reviews.get(key, TrainingReview()),
            metadata=metadata.get(key, TrainingMetadata()),
        )
        item.needs_attention = _needs_attention(item)
        items.append(item)
    return items


class UIMode(BaseModel):
    training_only: bool


@router.get("/ui-mode", response_model=UIMode)
def get_ui_mode() -> UIMode:
    """Whether this deployment should show only the Training page.

    Deliberately unauthenticated (fetched before login to decide nav/
    routing, same timing as `/api/auth/status`) and reveals nothing
    sensitive -- just a UI-shape flag for a single-purpose local
    deployment (e.g. a scratch machine only ever used to review synthetic
    training data), set via `KNOCK_TRAINING_MODE=1` in the environment.

    This *is* a security-relevant flag, unlike the name of this one field
    alone suggests: `require_auth` (`knock.api.auth_routes`) skips login
    entirely under the same env var, since a single-user local scratch
    box doesn't need a login screen standing between it and the one
    person who can already reach it. Anyone who can reach this port with
    `KNOCK_TRAINING_MODE=1` set can use the whole app, including triggering
    the script runner below -- don't set this on anything network-exposed.
    """
    return UIMode(training_only=training_mode_enabled())


class IntentOptions(BaseModel):
    intents: list[str]


@router.get("/intents", response_model=IntentOptions)
def get_training_intents(current_user: CurrentUserDep) -> IntentOptions:
    """The closed set of labels a correction's `intent_override` may use --
    the same set `_classification_prompt` itself offers, so the UI's
    correction dropdown can't drift from what's actually trainable.
    """
    return IntentOptions(intents=sorted(VALID_TRAINING_INTENTS))


@router.get("/queue", response_model=TrainingQueuePage)
def get_training_queue(
    current_user: CurrentUserDep,
    *,
    audit_log: AuditLogDep,
    review_store: ReviewStoreDep,
    metadata_store: MetadataStoreDep,
    status: Literal["pending", "approved", "rejected"] | None = Query(default=None),
    sort: Literal["newest", "needs_attention_first"] = Query(default="newest"),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=500),
) -> TrainingQueuePage:
    """A page of audit entries with something to train on (skips
    emergency/blocked requests, which never reach `classify_intent()`),
    each paired with its current review status and -- if the judge stage
    has scored it -- its judge/correction metadata.

    Filters by `status` (if given) *before* paginating, across the full
    history -- not a recent-N window truncated before filtering. That
    distinction matters once a deployment accumulates more than one
    page's worth of history: the previous version took a flat `limit`
    (capped at 1000) over the newest raw audit entries and filtered by
    status client-side, so anything reviewed earlier than that window
    -- most of an actually-reviewed history, in practice -- silently
    stopped appearing in the Approved/Rejected tabs at all, with no
    indication anything was missing.

    `sort="needs_attention_first"` puts every item `_needs_attention`
    flags (a safety veto, judge disagreement, or a low category/quality
    score) ahead of the rest, newest-first within each group -- lets a
    reviewer spend real attention on the ones that actually need a
    judgment call and move quickly through the rest, rather than hitting
    both in whatever order they happened to be generated.
    """
    matching = [
        item
        for item in _all_queue_items(audit_log, review_store, metadata_store)
        if status is None or item.review.status == status
    ]
    if sort == "needs_attention_first":
        matching.sort(key=lambda item: not item.needs_attention)
    total = len(matching)
    page = matching[offset : offset + limit]
    return TrainingQueuePage(
        items=page, total=total, offset=offset, limit=limit, has_more=offset + limit < total
    )


@router.get("/queue/counts", response_model=TrainingQueueCounts)
def get_training_queue_counts(
    current_user: CurrentUserDep,
    *,
    audit_log: AuditLogDep,
    review_store: ReviewStoreDep,
    metadata_store: MetadataStoreDep,
) -> TrainingQueueCounts:
    """Counts across the FULL history, for the tab labels -- computed in
    one pass so showing all three doesn't cost three separate full scans.
    """
    counts = {"pending": 0, "approved": 0, "rejected": 0}
    for item in _all_queue_items(audit_log, review_store, metadata_store):
        counts[item.review.status] += 1
    return TrainingQueueCounts(**counts)


class ReviewRequest(BaseModel):
    status: Literal["pending", "approved", "rejected"]
    intent_override: str | None = None
    response_override: str | None = None
    comment: str | None = None


@router.put("/queue/{key}", response_model=TrainingReview)
def set_training_review(
    key: str,
    body: ReviewRequest,
    current_user: CurrentUserDep,
    *,
    review_store: ReviewStoreDep,
) -> TrainingReview:
    if body.intent_override is not None and body.intent_override not in VALID_TRAINING_INTENTS:
        raise HTTPException(
            status_code=400,
            detail=f"intent_override must be one of {sorted(VALID_TRAINING_INTENTS)}",
        )
    review = TrainingReview(
        status=body.status,
        intent_override=body.intent_override,
        response_override=body.response_override,
        comment=body.comment,
    )
    review_store.set(key, review)
    return review


class SuggestCorrectionRequest(BaseModel):
    corrector_model: str
    # 2+ models, same requirement as the batch judge/correction scripts --
    # a single judge isn't a real ensemble. Re-judging is not optional
    # here: an unverified correction is worse than no correction, since
    # it'd otherwise look trustworthy in the edit box without actually
    # having cleared the bar a fresh candidate has to.
    judge_models: list[str]
    current_response: str
    # The reviewer's own instruction for the rewrite (e.g. "make this
    # shorter", "don't mention the dog") -- the Training page's comment
    # box. Additive to the judge's own flagged reason (if this entry has
    # been judged), not a replacement for it.
    human_note: str | None = None
    ollama_host: str = "127.0.0.1"
    ollama_port: int = 11434
    ollama_timeout: float = 120.0


class SuggestCorrectionResponse(BaseModel):
    corrected_response: str
    metadata: TrainingMetadata
    accepted: bool


@router.post("/queue/{key}/suggest-correction", response_model=SuggestCorrectionResponse)
def suggest_correction(
    key: str,
    body: SuggestCorrectionRequest,
    current_user: CurrentUserDep,
    *,
    audit_log: AuditLogDep,
    metadata_store: MetadataStoreDep,
) -> SuggestCorrectionResponse:
    """On-demand correction for one entry, triggered from the Training
    page -- an interactive counterpart to scripts/correct_training_data.py's
    batch flow: one corrector call, then re-judged through the full judge
    ensemble (not a cheaper check) before being trusted, same bar a fresh
    candidate has to clear. No retry loop, unlike the batch script -- a
    human is driving this one call at a time and can just click again
    (with a refined `human_note`) if the first attempt doesn't pass.

    Quotes the judge's own flagged reason back to the corrector the same
    way the batch script does, if this entry has been judged, plus the
    reviewer's own `human_note` if one was given.

    Writes the judge/correction *metadata* immediately (consistent with
    every other judging path in this project -- metadata always reflects
    the latest scoring, regardless of human review status). It does NOT
    touch review status or response_override: the corrected text is only
    returned for the reviewer's edit box to show, same as typing a
    correction by hand. An explicit Approve/Reject still has to follow
    before anything is actually used as training data.
    """
    if len(body.judge_models) < 2:
        raise HTTPException(
            status_code=400, detail="judge_models needs at least 2 models for a real ensemble"
        )

    entry = next(
        (e for e in audit_log.recent(limit=100_000) if example_key(e) == key),
        None,
    )
    if entry is None:
        raise HTTPException(status_code=404, detail="No audit entry with that key")

    meta = metadata_store.all().get(key) or TrainingMetadata()
    intent = entry.intent or "unknown"
    judge_reason = meta.judge.per_judge[0].reason if meta.judge and meta.judge.per_judge else ""

    def make_provider(model: str) -> OllamaProvider:
        return OllamaProvider(
            config=OllamaConfig(
                host=body.ollama_host,
                port=body.ollama_port,
                model=model,
                timeout=body.ollama_timeout,
            )
        )

    corrector = make_provider(body.corrector_model)
    judges = [(name, make_provider(name)) for name in body.judge_models]
    try:
        corrected = correct_response(
            corrector,
            entry.text,
            intent,
            body.current_response,
            judge_reason,
            human_note=body.human_note,
        )
        scores = [
            score
            for judge_name, judge_provider in judges
            if (
                score := score_candidate(
                    judge_provider,
                    judge_name,
                    entry.text,
                    intent,
                    corrected,
                    sorted(VALID_TRAINING_INTENTS),
                )
            )
            is not None
        ]
    finally:
        corrector.close()
        for _name, judge_provider in judges:
            judge_provider.close()

    result = aggregate_scores(scores)
    accepted = not result.voice_veto and not result.safety_veto
    meta.corrections = [
        *meta.corrections,
        CorrectionAttempt(
            attempt=len(meta.corrections) + 1,
            original_response=body.current_response,
            corrected_response=corrected,
            judge_reason=judge_reason or (body.human_note or ""),
            rejudged=result,
            accepted=accepted,
        ),
    ]
    if accepted:
        meta.judge = result
    metadata_store.set(key, meta)

    return SuggestCorrectionResponse(corrected_response=corrected, metadata=meta, accepted=accepted)


@router.get("/export")
def export_training_data(
    current_user: CurrentUserDep,
    *,
    audit_log: AuditLogDep,
    review_store: ReviewStoreDep,
    limit: int = Query(default=10000, ge=1, le=100000),
) -> PlainTextResponse:
    jsonl = export_training_jsonl(audit_log, review_store, limit=limit)
    return PlainTextResponse(
        content=jsonl,
        media_type="application/jsonl",
        headers={"Content-Disposition": 'attachment; filename="knock_training_data.jsonl"'},
    )


@router.get("/export/dpo")
def export_dpo_data(
    current_user: CurrentUserDep,
    *,
    audit_log: AuditLogDep,
    review_store: ReviewStoreDep,
    metadata_store: MetadataStoreDep,
    limit: int = Query(default=10000, ge=1, le=100000),
    margin_threshold: float = Query(default=2.0, ge=0.0, le=10.0),
) -> PlainTextResponse:
    jsonl = export_dpo_pairs_jsonl(
        audit_log, review_store, metadata_store, limit=limit, margin_threshold=margin_threshold
    )
    return PlainTextResponse(
        content=jsonl,
        media_type="application/jsonl",
        headers={"Content-Disposition": 'attachment; filename="knock_dpo_pairs.jsonl"'},
    )


# -- script runner: kick off the local synthetic-data scripts from the
# browser instead of a terminal (scripts/generate_scenarios.py,
# scripts/generate_training_data.py). See knock.core.script_runner for
# why this isn't built on BridgeSupervisor. --------------------------------


class ScriptStatus(BaseModel):
    script: str | None
    status: Literal["idle", "running", "completed", "failed", "stopped"]
    exit_code: int | None
    started_at: float | None


def _status_response(state: RunState) -> ScriptStatus:
    return ScriptStatus(
        script=state.script,
        status=state.status,
        exit_code=state.exit_code,
        started_at=state.started_at,
    )


@router.get("/scripts/status", response_model=ScriptStatus)
def get_script_status(current_user: CurrentUserDep, *, runner: ScriptRunnerDep) -> ScriptStatus:
    return _status_response(runner.state())


class ScriptLogs(BaseModel):
    lines: list[str]
    next_after: int


@router.get("/scripts/logs", response_model=ScriptLogs)
def get_script_logs(
    current_user: CurrentUserDep,
    *,
    runner: ScriptRunnerDep,
    after: int = Query(default=0, ge=0),
) -> ScriptLogs:
    lines, next_after = runner.tail(after)
    return ScriptLogs(lines=lines, next_after=next_after)


@router.post("/scripts/stop", response_model=ScriptStatus)
def stop_script(current_user: CurrentUserDep, *, runner: ScriptRunnerDep) -> ScriptStatus:
    runner.stop()
    return _status_response(runner.state())


def _start(runner: ScriptRunner, script: ScriptName, args: list[str]) -> None:
    # ScriptRunner.start() itself raises under its own lock if a run is
    # already in progress -- that's the real guard against two near-
    # simultaneous requests both starting a script; this just turns it
    # into a clean 409 instead of an unhandled 500.
    try:
        runner.start(script, args)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


class GenerateScenariosRequest(BaseModel):
    models: list[str]
    categories: list[str] | None = None
    count_per_category: int = 15
    batch_size: int = 5
    ollama_host: str = "127.0.0.1"
    ollama_port: int = 11434
    ollama_timeout: float = 120.0
    output: str = "scripts/training_scenarios.generated.txt"


@router.post("/scripts/generate-scenarios", response_model=ScriptStatus)
def start_generate_scenarios(
    body: GenerateScenariosRequest, current_user: CurrentUserDep, *, runner: ScriptRunnerDep
) -> ScriptStatus:
    if not body.models:
        raise HTTPException(status_code=400, detail="models must not be empty")
    args = [
        "--models",
        ",".join(body.models),
        "--count-per-category",
        str(body.count_per_category),
        "--batch-size",
        str(body.batch_size),
        "--ollama-host",
        body.ollama_host,
        "--ollama-port",
        str(body.ollama_port),
        "--ollama-timeout",
        str(body.ollama_timeout),
        "--output",
        body.output,
    ]
    if body.categories:
        args += ["--categories", ",".join(body.categories)]
    _start(runner, "generate_scenarios", args)
    return _status_response(runner.state())


class GenerateTrainingDataRequest(BaseModel):
    models: list[str]
    scenarios: str = "scripts/training_scenarios.txt"
    ollama_host: str = "127.0.0.1"
    ollama_port: int = 11434
    ollama_timeout: float = 120.0
    repeats: int = 1
    audit_log: str | None = None


@router.post("/scripts/generate-training-data", response_model=ScriptStatus)
def start_generate_training_data(
    body: GenerateTrainingDataRequest, current_user: CurrentUserDep, *, runner: ScriptRunnerDep
) -> ScriptStatus:
    if not body.models:
        raise HTTPException(status_code=400, detail="models must not be empty")
    args = [
        "--models",
        ",".join(body.models),
        "--scenarios",
        body.scenarios,
        "--ollama-host",
        body.ollama_host,
        "--ollama-port",
        str(body.ollama_port),
        "--ollama-timeout",
        str(body.ollama_timeout),
        "--repeats",
        str(body.repeats),
    ]
    if body.audit_log:
        args += ["--audit-log", body.audit_log]
    _start(runner, "generate_training_data", args)
    return _status_response(runner.state())


class JudgeTrainingDataRequest(BaseModel):
    judge_models: list[str]
    ollama_host: str = "127.0.0.1"
    ollama_port: int = 11434
    ollama_timeout: float = 120.0
    audit_log: str | None = None
    metadata_store: str | None = None
    limit: int | None = None


@router.post("/scripts/judge-training-data", response_model=ScriptStatus)
def start_judge_training_data(
    body: JudgeTrainingDataRequest, current_user: CurrentUserDep, *, runner: ScriptRunnerDep
) -> ScriptStatus:
    if len(body.judge_models) < 2:
        raise HTTPException(status_code=400, detail="judge_models needs at least 2 models")
    args = [
        "--judge-models",
        ",".join(body.judge_models),
        "--ollama-host",
        body.ollama_host,
        "--ollama-port",
        str(body.ollama_port),
        "--ollama-timeout",
        str(body.ollama_timeout),
    ]
    if body.audit_log:
        args += ["--audit-log", body.audit_log]
    if body.metadata_store:
        args += ["--metadata-store", body.metadata_store]
    if body.limit is not None:
        args += ["--limit", str(body.limit)]
    _start(runner, "judge_training_data", args)
    return _status_response(runner.state())


class CorrectTrainingDataRequest(BaseModel):
    corrector_model: str
    judge_models: list[str]
    max_attempts: int = 2
    ollama_host: str = "127.0.0.1"
    ollama_port: int = 11434
    ollama_timeout: float = 120.0
    audit_log: str | None = None
    metadata_store: str | None = None
    limit: int | None = None


@router.post("/scripts/correct-training-data", response_model=ScriptStatus)
def start_correct_training_data(
    body: CorrectTrainingDataRequest, current_user: CurrentUserDep, *, runner: ScriptRunnerDep
) -> ScriptStatus:
    if len(body.judge_models) < 2:
        raise HTTPException(status_code=400, detail="judge_models needs at least 2 models")
    args = [
        "--corrector-model",
        body.corrector_model,
        "--judge-models",
        ",".join(body.judge_models),
        "--max-attempts",
        str(body.max_attempts),
        "--ollama-host",
        body.ollama_host,
        "--ollama-port",
        str(body.ollama_port),
        "--ollama-timeout",
        str(body.ollama_timeout),
    ]
    if body.audit_log:
        args += ["--audit-log", body.audit_log]
    if body.metadata_store:
        args += ["--metadata-store", body.metadata_store]
    if body.limit is not None:
        args += ["--limit", str(body.limit)]
    _start(runner, "correct_training_data", args)
    return _status_response(runner.state())
