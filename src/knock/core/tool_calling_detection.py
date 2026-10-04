"""Resolves `OllamaConfig.use_tool_calling`'s tri-state into a concrete bool
at bridge startup -- see `OllamaConfig`'s own docstring on the tri-state
story (None = auto-detect, explicit True/False = a sticky operator
override that skips detection entirely).

Auto-detection is two-stage: a model can report Ollama's "tools" capability
without reliably producing a well-formed tool call in practice, so the
static flag alone isn't trusted -- only a live functional smoke test is.
Both stages fail closed (return False) on any error, since the plain-text
classification path this feeds into is the one that's actually battle-
tested.
"""

from __future__ import annotations

import logging

import httpx

from knock.config import OllamaConfig
from knock.providers.llm.ollama import OllamaProvider

logger = logging.getLogger(__name__)

_PROBE_TOOL = {
    "type": "function",
    "function": {
        "name": "ping",
        "description": "Call this to confirm tool-calling works.",
        "parameters": {"type": "object", "properties": {}},
    },
}


def detect_tool_calling_support(config: OllamaConfig) -> bool:
    """Only meaningful to call when `config.use_tool_calling` is None
    ("auto") -- an explicit True/False should skip this entirely (see
    `resolve_tool_calling`).
    """
    try:
        response = httpx.post(
            f"{config.base_url}/api/show", json={"name": config.model}, timeout=10.0
        )
        response.raise_for_status()
        capabilities = response.json().get("capabilities") or []
    except Exception as exc:  # noqa: BLE001 - detection is best-effort, fails closed
        logger.warning("Tool-calling capability check failed for %r: %s", config.model, exc)
        return False

    if "tools" not in capabilities:
        logger.info(
            "Model %r does not report tool-calling support -- staying on plain-text "
            "classification.",
            config.model,
        )
        return False

    provider = OllamaProvider(config=config)
    try:
        result = provider.chat_with_tools(
            [{"role": "user", "content": "Call the ping tool now."}], [_PROBE_TOOL]
        )
    except Exception as exc:  # noqa: BLE001 - detection is best-effort, fails closed
        logger.warning("Live tool-calling smoke test failed for %r: %s", config.model, exc)
        return False
    finally:
        provider.close()

    works = any(call.name == "ping" for call in result.tool_calls)
    if not works:
        logger.warning(
            "Model %r claims tool-calling support but didn't produce a valid call in "
            "the live test -- staying on plain-text classification.",
            config.model,
        )
    return works


def resolve_tool_calling(config: OllamaConfig) -> bool:
    """What every bridge's main() should call -- handles the tri-state
    dispatch so callers never need to check `config.use_tool_calling`
    themselves.
    """
    if config.use_tool_calling is not None:
        return config.use_tool_calling
    return detect_tool_calling_support(config)
