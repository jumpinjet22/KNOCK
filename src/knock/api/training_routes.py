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
from knock.core.audit import AuditEntry, JSONLAuditLog
from knock.core.script_runner import RunState, ScriptName, ScriptRunner
from knock.core.training import (
    VALID_TRAINING_INTENTS,
    TrainingReview,
    TrainingReviewStore,
    example_key,
    export_training_jsonl,
    training_mode_enabled,
)

router = APIRouter(prefix="/api/training", tags=["training"])

_script_runner = ScriptRunner()


def get_audit_log() -> JSONLAuditLog:
    return JSONLAuditLog()


def get_review_store() -> TrainingReviewStore:
    return TrainingReviewStore()


def get_script_runner() -> ScriptRunner:
    return _script_runner


AuditLogDep = Annotated[JSONLAuditLog, Depends(get_audit_log)]
ReviewStoreDep = Annotated[TrainingReviewStore, Depends(get_review_store)]
ScriptRunnerDep = Annotated[ScriptRunner, Depends(get_script_runner)]


class TrainingQueueItem(BaseModel):
    key: str
    entry: AuditEntry
    review: TrainingReview


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


@router.get("/queue", response_model=list[TrainingQueueItem])
def get_training_queue(
    current_user: CurrentUserDep,
    *,
    audit_log: AuditLogDep,
    review_store: ReviewStoreDep,
    limit: int = Query(default=200, ge=1, le=1000),
) -> list[TrainingQueueItem]:
    """Every audit entry with something to train on (skips emergency/
    blocked requests, which never reach `classify_intent()`), newest
    first, each paired with its current review status.
    """
    reviews = review_store.all()
    items: list[TrainingQueueItem] = []
    for entry in audit_log.recent(limit=limit):
        if entry.intent is None:
            continue
        key = example_key(entry)
        items.append(
            TrainingQueueItem(key=key, entry=entry, review=reviews.get(key, TrainingReview()))
        )
    return items


class ReviewRequest(BaseModel):
    status: Literal["pending", "approved", "rejected"]
    intent_override: str | None = None
    response_override: str | None = None


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
    )
    review_store.set(key, review)
    return review


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
