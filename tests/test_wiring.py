from unittest.mock import patch

from knock.config import OllamaConfig, VisionConfig
from knock.providers.llm.ollama import OllamaProvider
from knock.providers.vision.ollama import OllamaVisionProvider
from knock.wiring import build_orchestrator


def _build(ollama: OllamaConfig, vision: VisionConfig | None = None):
    # warm_up() would call a real Ollama server.
    with (
        patch.object(OllamaProvider, "warm_up") as llm_warm,
        patch.object(OllamaVisionProvider, "warm_up") as vision_warm,
    ):
        return build_orchestrator(ollama, vision), llm_warm, vision_warm


def test_without_vision_there_is_no_vision_provider_and_no_image_for_the_brain() -> None:
    (orchestrator, vision_provider), llm_warm, vision_warm = _build(OllamaConfig())

    assert vision_provider is None
    assert orchestrator.send_image_to_brain is False
    llm_warm.assert_called_once()
    vision_warm.assert_not_called()


def test_with_vision_the_provider_is_built_warmed_and_the_image_flag_follows_config() -> None:
    (orchestrator, vision_provider), llm_warm, vision_warm = _build(
        OllamaConfig(), VisionConfig(send_image_to_brain=True)
    )

    assert isinstance(vision_provider, OllamaVisionProvider)
    assert orchestrator.send_image_to_brain is True
    vision_warm.assert_called_once()
    llm_warm.assert_called_once()


def test_the_second_opinion_model_is_only_built_when_configured() -> None:
    (orchestrator, _), llm_warm, _ = _build(OllamaConfig())
    assert orchestrator.safety_check_provider is None
    assert llm_warm.call_count == 1

    (orchestrator, _), llm_warm, _ = _build(OllamaConfig(safety_check_model="bigger-model"))
    assert orchestrator.safety_check_provider is not None
    assert llm_warm.call_count == 2
