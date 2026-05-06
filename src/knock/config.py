from pydantic import BaseModel, Field


class KnockConfig(BaseModel):
    """Application configuration with safe defaults."""

    app_name: str = "KNOCK"
    short_response_limit: int = Field(default=140, ge=20, le=500)
