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
from knock.core.training import (
    VALID_TRAINING_INTENTS,
    TrainingReview,
    TrainingReviewStore,
    example_key,
    export_training_jsonl,
)

router = APIRouter(prefix="/api/training", tags=["training"])


def get_audit_log() -> JSONLAuditLog:
    return JSONLAuditLog()


def get_review_store() -> TrainingReviewStore:
    return TrainingReviewStore()


AuditLogDep = Annotated[JSONLAuditLog, Depends(get_audit_log)]
ReviewStoreDep = Annotated[TrainingReviewStore, Depends(get_review_store)]


class TrainingQueueItem(BaseModel):
    key: str
    entry: AuditEntry
    review: TrainingReview


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
