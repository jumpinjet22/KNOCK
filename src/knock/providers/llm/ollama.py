from __future__ import annotations

import re

import httpx

from knock.config import OllamaConfig
from knock.conversation.prompts import SYSTEM_PROMPT

# Some reasoning models (e.g. deepseek-r1) always emit a literal <think>...
# </think> block ahead of their real answer, ignoring the "think": False
# request below -- that option only suppresses hidden reasoning for models
# Ollama has a dedicated toggle for (e.g. qwen3.5). Left unstripped, a long
# reasoning block routinely overruns PolicyEngine.apply_style()'s 140-char
# cap before the real answer even starts, so every response comes out as
# truncated chain-of-thought instead of an actual reply.
_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


def _strip_thinking(text: str) -> str:
    text = _THINK_BLOCK_RE.sub("", text)
    # An unterminated <think> means the model was cut off mid-reasoning and
    # never produced a real answer -- discard it rather than leak the
    # dangling reasoning fragment. Callers already treat an empty result as
    # "no usable response" and fall back to a safe default.
    if "<think>" in text:
        text = text.split("<think>", 1)[0]
    return text.strip()


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
        return _strip_thinking(str(data.get("response", "")))

    def close(self) -> None:
        self._client.close()
