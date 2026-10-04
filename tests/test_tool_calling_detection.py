import httpx
import respx

from knock.config import OllamaConfig
from knock.core.tool_calling_detection import detect_tool_calling_support, resolve_tool_calling


@respx.mock
def test_detect_returns_true_when_capability_and_live_check_both_pass() -> None:
    config = OllamaConfig()
    respx.post(f"{config.base_url}/api/show").mock(
        return_value=httpx.Response(200, json={"capabilities": ["completion", "tools"]})
    )
    respx.post(f"{config.base_url}/api/chat").mock(
        return_value=httpx.Response(
            200, json={"message": {"tool_calls": [{"function": {"name": "ping", "arguments": {}}}]}}
        )
    )

    assert detect_tool_calling_support(config) is True


@respx.mock
def test_detect_returns_false_when_model_does_not_report_tools_capability() -> None:
    config = OllamaConfig()
    respx.post(f"{config.base_url}/api/show").mock(
        return_value=httpx.Response(200, json={"capabilities": ["completion"]})
    )
    chat_route = respx.post(f"{config.base_url}/api/chat").mock(
        return_value=httpx.Response(200, json={"message": {"tool_calls": []}})
    )

    assert detect_tool_calling_support(config) is False
    assert not chat_route.called  # never even attempts the live check


@respx.mock
def test_detect_returns_false_when_capability_check_itself_fails() -> None:
    config = OllamaConfig()
    respx.post(f"{config.base_url}/api/show").mock(return_value=httpx.Response(500))

    assert detect_tool_calling_support(config) is False


@respx.mock
def test_detect_returns_false_when_model_claims_tools_but_live_check_fails() -> None:
    config = OllamaConfig()
    respx.post(f"{config.base_url}/api/show").mock(
        return_value=httpx.Response(200, json={"capabilities": ["tools"]})
    )
    respx.post(f"{config.base_url}/api/chat").mock(return_value=httpx.Response(500))

    assert detect_tool_calling_support(config) is False


@respx.mock
def test_detect_returns_false_when_model_claims_tools_but_never_calls_the_probe() -> None:
    config = OllamaConfig()
    respx.post(f"{config.base_url}/api/show").mock(
        return_value=httpx.Response(200, json={"capabilities": ["tools"]})
    )
    respx.post(f"{config.base_url}/api/chat").mock(
        return_value=httpx.Response(200, json={"message": {"content": "sure, ok"}})
    )

    assert detect_tool_calling_support(config) is False


def test_resolve_skips_detection_when_explicitly_true() -> None:
    config = OllamaConfig(use_tool_calling=True)
    # No respx mocks set up at all -- if resolve_tool_calling tried to hit
    # the network, this would raise a connection error.
    assert resolve_tool_calling(config) is True


def test_resolve_skips_detection_when_explicitly_false() -> None:
    config = OllamaConfig(use_tool_calling=False)
    assert resolve_tool_calling(config) is False


@respx.mock
def test_resolve_runs_detection_when_unset() -> None:
    config = OllamaConfig(use_tool_calling=None)
    respx.post(f"{config.base_url}/api/show").mock(
        return_value=httpx.Response(200, json={"capabilities": ["tools"]})
    )
    respx.post(f"{config.base_url}/api/chat").mock(
        return_value=httpx.Response(
            200, json={"message": {"tool_calls": [{"function": {"name": "ping", "arguments": {}}}]}}
        )
    )

    assert resolve_tool_calling(config) is True
