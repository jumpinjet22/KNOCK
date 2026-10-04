from pydantic import BaseModel, Field


class ResponseDecision(BaseModel):
    """Final response selected by orchestrator."""

    text: str
    safe: bool = True
    escalate: bool = False
    reason: str = "normal"
    # What actually produced this response -- "emergency", a blocked
    # request's reason (e.g. "occupancy"), or classify_intent()'s category
    # for an allowed message. Lets a caller react to specific situations
    # (e.g. UnifiBridge notifying the household on a signature-required
    # delivery) without re-deriving the intent itself.
    intent: str | None = None
    # Set only from Orchestrator's tool-calling classification path (see
    # orchestrator.py's _refine_unknown_intent_via_tools) when no single
    # category was a clear/confident winner. Never set outside the
    # "normal" (allowed, non-emergency) branch of respond(), so these can
    # never influence the block/escalate decision itself -- consumed only
    # by the bridges' *notification* logic.
    needs_review: bool = False
    review_candidates: list[str] = Field(default_factory=list)
    review_summary: str | None = None
