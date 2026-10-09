from datetime import datetime

from pydantic import BaseModel, Field

from knock.core.scene import SceneContext


class VisitorEvent(BaseModel):
    """Incoming visitor-facing event.

    `text` is only ever what the visitor said (or a plain trigger
    description when there's no speech) -- camera output goes in `scene`
    instead, so the deterministic policy/intent layers never mistake it for
    speech. See `knock.core.scene` for why the two are kept apart.
    """

    source: str = Field(default="doorbell")
    text: str = Field(min_length=1)
    timestamp: datetime
    scene: SceneContext | None = None
