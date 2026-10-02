from __future__ import annotations

import base64
import io
import logging

import httpx
from PIL import Image, UnidentifiedImageError

from knock.config import VisionConfig
from knock.providers.vision.safety import sanitize_description

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


def _resize_for_model(image: bytes) -> bytes:
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
        encoded_image = base64.b64encode(_resize_for_model(image)).decode("ascii")
        response = self._client.post(
            f"{self.config.base_url}/api/generate",
            json={
                "model": self.config.model,
                "prompt": prompt or self.config.prompt,
                "images": [encoded_image],
                "stream": False,
                # See OllamaProvider.generate()'s same option -- a
                # reasoning-capable vision model spends extra time on a
                # hidden chain-of-thought by default; a doorbell snapshot
                # description doesn't need it.
                "think": False,
            },
        )
        response.raise_for_status()
        data = response.json()
        return str(data.get("response", "")).strip()

    def describe(self, image: bytes, prompt: str | None = None) -> str:
        return sanitize_description(self.describe_raw(image, prompt))

    def close(self) -> None:
        self._client.close()
