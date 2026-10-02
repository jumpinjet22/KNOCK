from __future__ import annotations

import base64

import httpx

from knock.config import VisionConfig
from knock.providers.vision.safety import sanitize_description


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
        encoded_image = base64.b64encode(image).decode("ascii")
        response = self._client.post(
            f"{self.config.base_url}/api/generate",
            json={
                "model": self.config.model,
                "prompt": prompt or self.config.prompt,
                "images": [encoded_image],
                "stream": False,
            },
        )
        response.raise_for_status()
        data = response.json()
        return str(data.get("response", "")).strip()

    def describe(self, image: bytes, prompt: str | None = None) -> str:
        return sanitize_description(self.describe_raw(image, prompt))

    def close(self) -> None:
        self._client.close()
