from typing import Protocol

from knock.core.scene import PackageLabel, SceneObservation
from knock.providers.vision.barcode import decode_tracking_barcode


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
    """A structured shipping-label read from any vision provider, enriched
    with a deterministic barcode decode (see `barcode.decode_tracking_barcode`)
    whenever one confidently identifies a carrier and tracking number --
    that beats the vision model's own guess for those two fields, since a
    barcode is either decoded exactly or not at all, never character-level
    wrong the way small-print OCR can be.

    None only when the provider has no `read_package_label` capability (or
    it returned nothing) AND no barcode was confidently decoded either --
    i.e. there's nothing at all to report.
    """
    read = getattr(provider, "read_package_label", None)
    label = None
    if callable(read):
        result = read(image)
        if isinstance(result, PackageLabel):
            label = result

    barcode_match = decode_tracking_barcode(image)
    if barcode_match is not None:
        carrier, tracking_number = barcode_match
        label = (label or PackageLabel()).model_copy(
            update={"carrier": carrier, "tracking_number": tracking_number}
        )

    if label is None:
        return None
    if label.carrier is None and label.tracking_number is None and label.recipient_name is None:
        return None
    return label
