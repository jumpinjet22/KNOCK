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
            },
        )
        response.raise_for_status()
        data = response.json()
        return str(data.get("response", "")).strip()

    def close(self) -> None:
        self._client.close()
