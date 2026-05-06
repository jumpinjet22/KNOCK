from datetime import datetime

from pydantic import BaseModel, Field


class SessionState(BaseModel):
    """Minimal mutable session state."""

    session_id: str
    turn_count: int = 0
    last_intent: str = "unknown"
    escalated: bool = False
    updated_at: datetime
    history: list[str] = Field(default_factory=list)
