from __future__ import annotations

import httpx

from knock.config import OllamaConfig
from knock.conversation.prompts import SYSTEM_PROMPT


class OllamaProvider:
    """LLM provider backed by a local Ollama server (https://ollama.com)."""

    name = "ollama"

    def __init__(
        self,
        config: OllamaConfig | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.config = config or OllamaConfig()
        self._client = client or httpx.Client(timeout=self.config.timeout)

    def generate(self, prompt: str) -> str:
        response = self._client.post(
            f"{self.config.base_url}/api/generate",
            json={
                "model": self.config.model,
                "system": SYSTEM_PROMPT,
                "prompt": prompt,
                "stream": False,
                # A reasoning-capable model (e.g. qwen3.5) spends many
                # seconds on a hidden chain-of-thought before answering by
                # default -- measured 24.6s vs. 0.6s for the same prompt
                # with this off, no loss in response quality for KNOCK's
                # short-reply use case. Ignored by models that don't
                # support thinking at all.
                "think": False,
            },
        )
        response.raise_for_status()
        data = response.json()
        return str(data.get("response", "")).strip()

    def close(self) -> None:
        self._client.close()
