from pydantic import BaseModel, Field


class ConversationContext(BaseModel):
    """Conversation context for one response turn."""

    visitor_text: str
    intent: str = "unknown"
    policy_flags: list[str] = Field(default_factory=list)
