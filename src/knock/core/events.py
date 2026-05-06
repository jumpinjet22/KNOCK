from datetime import datetime

from pydantic import BaseModel, Field


class VisitorEvent(BaseModel):
    """Incoming visitor-facing event."""

    source: str = Field(default="doorbell")
    text: str = Field(min_length=1)
    timestamp: datetime
