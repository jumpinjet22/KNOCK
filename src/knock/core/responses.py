from pydantic import BaseModel


class ResponseDecision(BaseModel):
    """Final response selected by orchestrator."""

    text: str
    safe: bool = True
    escalate: bool = False
    reason: str = "normal"
