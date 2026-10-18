from typing import Protocol

from knock.core.scene import PackageLabel, SceneObservation


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


def read_package_label(provider: VisionProvider, image: bytes) -> PackageLabel | None:
    """A structured shipping-label read from any vision provider, or None
    if the provider has no such capability (e.g. a provider that only
    implements the original `describe()` contract) or nothing was legible.
    """
    read = getattr(provider, "read_package_label", None)
    if callable(read):
        result = read(image)
        if isinstance(result, PackageLabel):
            return result
    return None
