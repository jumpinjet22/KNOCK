from __future__ import annotations

import base64
import logging
import re
from typing import Any

import httpx
from pydantic import BaseModel, Field

from knock.config import OllamaConfig, keep_alive_payload
from knock.conversation.prompts import SYSTEM_PROMPT
from knock.providers.vision.ollama import resize_for_model

# Some reasoning models (e.g. deepseek-r1) always emit a literal <think>...
# </think> block ahead of their real answer, ignoring the "think": False
# request below -- that option only suppresses hidden reasoning for models
# Ollama has a dedicated toggle for (e.g. qwen3.5). Left unstripped, a long
# reasoning block routinely overruns PolicyEngine.apply_style()'s 140-char
# cap before the real answer even starts, so every response comes out as
# truncated chain-of-thought instead of an actual reply.
_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)

logger = logging.getLogger(__name__)


def _strip_thinking(text: str) -> str:
    text = _THINK_BLOCK_RE.sub("", text)
    # An unterminated <think> means the model was cut off mid-reasoning and
    # never produced a real answer -- discard it rather than leak the
    # dangling reasoning fragment. Callers already treat an empty result as
    # "no usable response" and fall back to a safe default.
    if "<think>" in text:
        text = text.split("<think>", 1)[0]
    return text.strip()


class ToolCall(BaseModel):
    """One function call Ollama's `/api/chat` asked the caller to run."""

    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ChatToolResult(BaseModel):
    """Result of a `chat_with_tools` call -- any tool calls the model made,
    plus whatever plain text it answered with (often empty when it called a
    tool instead of replying directly).
    """

    tool_calls: list[ToolCall] = Field(default_factory=list)
    content: str = ""


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

    def _with_keep_alive(self, payload: dict[str, Any]) -> dict[str, Any]:
        # Only sent when configured, so the server's own OLLAMA_KEEP_ALIVE
        # (or its 5-minute default) stays in charge otherwise.
        keep_alive = keep_alive_payload(self.config.keep_alive)
        if keep_alive is not None:
            payload["keep_alive"] = keep_alive
        return payload

    def generate(self, prompt: str) -> str:
        return self.generate_with_images(prompt, [])

    def generate_with_images(self, prompt: str, images: list[bytes]) -> str:
        """`generate()`, plus raw images for a multimodal model -- only used
        by `Orchestrator`'s opt-in `send_image_to_brain` path.
        """
        payload: dict[str, Any] = {
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
        }
        if images:
            payload["images"] = [
                base64.b64encode(resize_for_model(image)).decode("ascii") for image in images
            ]
        response = self._client.post(
            f"{self.config.base_url}/api/generate", json=self._with_keep_alive(payload)
        )
        response.raise_for_status()
        data = response.json()
        return _strip_thinking(str(data.get("response", "")))

    def warm_up(self) -> bool:
        """Load the model into memory ahead of the first real request.

        An empty-prompt `/api/generate` call is Ollama's documented way to
        load a model without generating anything; paired with `keep_alive`,
        the first doorbell ring after startup doesn't pay the model-load
        cost. Best-effort: returns False (and logs) on any failure rather
        than raising, since a bridge must still start with Ollama down.
        """
        try:
            response = self._client.post(
                f"{self.config.base_url}/api/generate",
                json=self._with_keep_alive({"model": self.config.model}),
            )
            response.raise_for_status()
        except Exception as exc:  # noqa: BLE001 - warm-up is best-effort
            logger.warning("Ollama warm-up for %r failed: %s", self.config.model, exc)
            return False
        return True

    def chat_with_tools(
        self, messages: list[dict[str, str]], tools: list[dict[str, Any]]
    ) -> ChatToolResult:
        """Structured tool-calling via Ollama's `/api/chat`, unlike `generate()`'s
        plain `/api/generate` -- used where a caller needs a reliably-shaped
        answer (e.g. a classification from a fixed set of categories) instead
        of free text to exact-match against.
        """
        response = self._client.post(
            f"{self.config.base_url}/api/chat",
            json=self._with_keep_alive(
                {
                    "model": self.config.model,
                    "messages": messages,
                    "tools": tools,
                    "stream": False,
                    "think": False,
                }
            ),
        )
        response.raise_for_status()
        message = response.json().get("message") or {}
        tool_calls = [
            ToolCall(
                name=str((call.get("function") or {}).get("name", "")),
                arguments=(call.get("function") or {}).get("arguments") or {},
            )
            for call in message.get("tool_calls") or []
        ]
        return ChatToolResult(
            tool_calls=tool_calls, content=_strip_thinking(str(message.get("content", "")))
        )

    def close(self) -> None:
        self._client.close()
