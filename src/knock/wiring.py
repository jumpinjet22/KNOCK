"""Building the pieces every entry point needs, in one place.

The UniFi and Home Assistant console scripts each assembled the same
Ollama providers (answer model, optional second-opinion model, optional
vision model), warmed them up and handed them to an `Orchestrator`. That
recipe lives here so an entry point only says which connector it runs.
"""

from __future__ import annotations

from knock.config import OllamaConfig, VisionConfig
from knock.core.orchestrator import Orchestrator
from knock.core.tool_calling_detection import resolve_tool_calling
from knock.providers.llm.ollama import OllamaProvider
from knock.providers.vision.ollama import OllamaVisionProvider


def build_orchestrator(
    ollama_config: OllamaConfig, vision_config: VisionConfig | None = None
) -> tuple[Orchestrator, OllamaVisionProvider | None]:
    """The orchestrator for an entry point, plus its vision provider when
    `vision_config` is given (None -> no vision, as the Home Assistant
    bridge runs). Every model is loaded now rather than on the first
    visitor -- see `warm_up()`.
    """
    # None (unset) leaves the second-opinion layer off -- the deterministic
    # apply_style() backstop still applies regardless. When set, this is a
    # second OllamaProvider pointed at a different model (and optionally a
    # different Ollama server, see OllamaConfig.safety_check_config) so a
    # bigger/more careful model can be used for this one extra-opinion call
    # without slowing down every-turn response phrasing.
    safety_check_config = ollama_config.safety_check_config()
    safety_check_provider = (
        OllamaProvider(config=safety_check_config) if safety_check_config is not None else None
    )
    llm_provider = OllamaProvider(config=ollama_config)
    vision_provider = OllamaVisionProvider(config=vision_config) if vision_config else None
    for provider in (llm_provider, vision_provider, safety_check_provider):
        if provider is not None:
            provider.warm_up()
    orchestrator = Orchestrator(
        llm_provider=llm_provider,
        use_tool_calling=resolve_tool_calling(ollama_config),
        safety_check_provider=safety_check_provider,
        send_image_to_brain=vision_config.send_image_to_brain if vision_config else False,
    )
    return orchestrator, vision_provider
