from typing import Protocol

from knock.core.scene import SceneObservation


class VisionProvider(Protocol):
    name: str

    def describe(self, image: bytes, prompt: str | None = None) -> str: ...


def observe_scene(provider: VisionProvider, image: bytes) -> SceneObservation:
    """A structured observation from any vision provider.

    Uses the provider's own `observe()` when it has one (e.g.
    `OllamaVisionProvider`); a provider that only implements the original
    `describe()` contract still works, with its one sentence as `summary`.
    """
    observe = getattr(provider, "observe", None)
    if callable(observe):
        observation = observe(image)
        if isinstance(observation, SceneObservation):
            return observation
    return SceneObservation(summary=provider.describe(image))
