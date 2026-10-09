from __future__ import annotations

import base64
import io
import json
import logging
from typing import Any

import httpx
from PIL import Image, UnidentifiedImageError

from knock.config import VisionConfig, keep_alive_payload
from knock.core.scene import SceneObservation
from knock.providers.vision.safety import contains_alarming_language, sanitize_description

logger = logging.getLogger(__name__)

# A full-resolution phone/doorbell-camera photo (often 3000px+ on a side)
# tokenizes to more than small self-hosted servers' context windows allow --
# seen in practice as a 400 "request exceeds the available context size"
# from the model server, on a camera that otherwise works fine. A doorbell
# description task ("a person", "a package", "a vehicle") doesn't need full
# resolution, so downscale before sending rather than asking everyone
# running a small context window to special-case this.
_MAX_IMAGE_DIMENSION = 1024
_RESIZE_JPEG_QUALITY = 85


def resize_for_model(image: bytes) -> bytes:
    """Downscale `image` so neither side exceeds `_MAX_IMAGE_DIMENSION`.

    Returns `image` unchanged if it's already small enough, or if it can't
    be parsed as an image at all -- resizing is a best-effort optimization,
    never a reason to block an otherwise-valid request.
    """
    try:
        with Image.open(io.BytesIO(image)) as img:
            if img.width <= _MAX_IMAGE_DIMENSION and img.height <= _MAX_IMAGE_DIMENSION:
                return image
            converted = img.convert("RGB")
            converted.thumbnail(
                (_MAX_IMAGE_DIMENSION, _MAX_IMAGE_DIMENSION), Image.Resampling.LANCZOS
            )
            buffer = io.BytesIO()
            converted.save(buffer, format="JPEG", quality=_RESIZE_JPEG_QUALITY)
            return buffer.getvalue()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        logger.warning("Could not parse image for resizing (%s); sending it unchanged", exc)
        return image


# Structured-output prompt for `observe()`. Same "literal, neutral, no
# speculation" stance as `VisionConfig.prompt`, plus explicit "leave it
# empty" instructions so a field the model can't see stays blank instead of
# being invented to satisfy the schema.
_OBSERVE_PROMPT = (
    "Look at this doorbell camera image and fill in each field using only "
    "what is literally and plainly visible. people_count: number of people "
    "visible. carrying: short names of objects people are holding (e.g. "
    '"box", "bag", "clipboard"), empty list if none. package_visible: '
    "whether a parcel/box/package is visible anywhere. uniform_or_logo: a "
    "company name or uniform type clearly visible on clothing, else empty. "
    "vehicle: a short description of any vehicle in view (include a "
    "company name only if clearly printed on it), else empty. "
    "visible_text: any clearly legible printed text, copied exactly, else "
    "empty. summary: one short neutral sentence. Do not guess what any "
    "object contains, and do not describe or speculate about anyone's "
    "identity, face, age, emotion, intent, or about danger, weapons, or "
    "threats. If unsure about a field, leave it empty."
)

# Hand-written rather than `SceneObservation.model_json_schema()`: every
# field required with a plain (non-nullable) type is the shape small local
# models fill in most reliably under Ollama's grammar-constrained `format`.
# "" / [] mean "not visible"; `parse_observation` maps those back to None.
_OBSERVATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "people_count": {"type": "integer"},
        "carrying": {"type": "array", "items": {"type": "string"}},
        "package_visible": {"type": "boolean"},
        "uniform_or_logo": {"type": "string"},
        "vehicle": {"type": "string"},
        "visible_text": {"type": "string"},
        "summary": {"type": "string"},
    },
    "required": [
        "people_count",
        "carrying",
        "package_visible",
        "uniform_or_logo",
        "vehicle",
        "visible_text",
        "summary",
    ],
}

_MAX_CARRYING_ITEMS = 5


def _safe_text(value: object) -> str | None:
    """A cleaned free-text field, or None if blank or alarming.

    Dropping an alarming field outright (rather than substituting
    `sanitize_description`'s generic fallback sentence) keeps a sentence
    like "Something is visible at the door." from masquerading as, say,
    a uniform or a vehicle.
    """
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    if not cleaned:
        return None
    if contains_alarming_language(cleaned):
        logger.warning("Vision field flagged as alarming/speculative, dropping: %r", cleaned)
        return None
    return cleaned


def parse_observation(raw: str) -> SceneObservation:
    """Raises `ValueError` if `raw` isn't a JSON object -- the caller
    falls back to free-text `describe()` in that case.
    """
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("vision observation is not a JSON object")

    people_count = data.get("people_count")
    carrying_raw = data.get("carrying")
    package_visible = data.get("package_visible")
    carrying = [
        item
        for item in (
            _safe_text(entry) for entry in (carrying_raw if isinstance(carrying_raw, list) else [])
        )
        if item is not None
    ][:_MAX_CARRYING_ITEMS]
    summary = data.get("summary")

    return SceneObservation(
        people_count=(
            people_count
            if isinstance(people_count, int)
            and not isinstance(people_count, bool)
            and people_count >= 0
            else None
        ),
        carrying=carrying,
        package_visible=package_visible if isinstance(package_visible, bool) else None,
        uniform_or_logo=_safe_text(data.get("uniform_or_logo")),
        vehicle=_safe_text(data.get("vehicle")),
        visible_text=_safe_text(data.get("visible_text")),
        # The summary is the one field that reads as a sentence, so it gets
        # the same sentence-level fallback `describe()` does.
        summary=sanitize_description(summary.strip()) if isinstance(summary, str) else "",
    )


class OllamaVisionProvider:
    """Vision provider backed by a vision-capable model on a local Ollama server.

    Uses the same `/api/generate` endpoint as `OllamaProvider`, with an added
    base64-encoded `images` field -- see https://docs.ollama.com/capabilities/vision.
    Small/fast models (the default, `moondream`) trade some accuracy for
    doorbell-latency response times; swap in `llava` or `qwen2.5vl` via
    `VisionConfig.model` for better quality if your hardware has headroom.

    Every description is passed through `vision.safety.sanitize_description`
    before being returned, since vision models are known to hallucinate
    alarming content ("possibly a bomb") out of ambiguous shapes -- the
    default prompt (`VisionConfig.prompt`) also asks the model to describe
    only what's literally visible, but that's an instruction a model can
    ignore, not a guarantee.
    """

    name = "ollama-vision"

    def __init__(
        self,
        config: VisionConfig | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.config = config or VisionConfig()
        self._client = client or httpx.Client(timeout=self.config.timeout)

    def describe_raw(self, image: bytes, prompt: str | None = None) -> str:
        """Like `describe()`, but skips the safety filter.

        Only for the debug/test-lab UI's "raw vs. sanitized, side by side"
        panel -- real response paths must always go through `describe()`.
        """
        return self._generate(image, prompt or self.config.prompt)

    def _generate(self, image: bytes, prompt: str, schema: dict[str, Any] | None = None) -> str:
        encoded_image = base64.b64encode(resize_for_model(image)).decode("ascii")
        payload: dict[str, Any] = {
            "model": self.config.model,
            "prompt": prompt,
            "images": [encoded_image],
            "stream": False,
            # See OllamaProvider.generate()'s same option -- a
            # reasoning-capable vision model spends extra time on a
            # hidden chain-of-thought by default; a doorbell snapshot
            # description doesn't need it.
            "think": False,
        }
        if schema is not None:
            payload["format"] = schema
        keep_alive = keep_alive_payload(self.config.keep_alive)
        if keep_alive is not None:
            payload["keep_alive"] = keep_alive
        response = self._client.post(f"{self.config.base_url}/api/generate", json=payload)
        response.raise_for_status()
        data = response.json()
        return str(data.get("response", "")).strip()

    def describe(self, image: bytes, prompt: str | None = None) -> str:
        return sanitize_description(self.describe_raw(image, prompt))

    def observe_raw(self, image: bytes) -> str:
        """The model's raw structured-output JSON, unparsed and unfiltered --
        only for the debug UI's raw-vs-sanitized panel, like `describe_raw`.
        """
        return self._generate(image, _OBSERVE_PROMPT, _OBSERVATION_SCHEMA)

    def observe(self, image: bytes) -> SceneObservation:
        """A structured, safety-filtered observation of `image` -- what the
        bridges hand the brain (via `SceneContext`) instead of one pasted
        sentence.

        Uses Ollama's grammar-constrained structured output (`format` set
        to a JSON schema), so a well-behaved server returns schema-shaped
        JSON every time. If the reply still can't be parsed (an older
        Ollama, a model that ignores `format`), this falls back to plain
        `describe()` with only `summary` filled in -- never worse than the
        one-sentence behavior that existed before.
        """
        raw = self.observe_raw(image)
        try:
            return parse_observation(raw)
        except ValueError as exc:  # json.JSONDecodeError is a ValueError
            logger.warning("Structured vision output unparseable (%s); falling back", exc)
        return SceneObservation(summary=self.describe(image))

    def warm_up(self) -> bool:
        """Same as `OllamaProvider.warm_up` -- load the vision model ahead
        of the first ring. Best-effort; False on any failure.
        """
        payload: dict[str, Any] = {"model": self.config.model}
        keep_alive = keep_alive_payload(self.config.keep_alive)
        if keep_alive is not None:
            payload["keep_alive"] = keep_alive
        try:
            response = self._client.post(f"{self.config.base_url}/api/generate", json=payload)
            response.raise_for_status()
        except Exception as exc:  # noqa: BLE001 - warm-up is best-effort
            logger.warning("Vision warm-up for %r failed: %s", self.config.model, exc)
            return False
        return True

    def close(self) -> None:
        self._client.close()
