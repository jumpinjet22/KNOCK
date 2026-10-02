from pydantic import BaseModel


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
