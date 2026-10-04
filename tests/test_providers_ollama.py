import httpx
import pytest
import respx

from knock.config import OllamaConfig
from knock.providers.llm.ollama import OllamaProvider


@respx.mock
def test_generate_returns_trimmed_response_text() -> None:
    config = OllamaConfig(host="127.0.0.1", port=11434, model="llama3.2")
    route = respx.post(f"{config.base_url}/api/generate").mock(
        return_value=httpx.Response(200, json={"response": "  Hello there  "})
    )

    provider = OllamaProvider(config=config)
    result = provider.generate("hi")

    assert result == "Hello there"
    assert route.called
    sent_body = route.calls.last.request.content
    assert b"llama3.2" in sent_body
    assert b"hi" in sent_body


@respx.mock
def test_generate_sends_system_prompt() -> None:
    config = OllamaConfig()
    route = respx.post(f"{config.base_url}/api/generate").mock(
        return_value=httpx.Response(200, json={"response": "ok"})
    )

    OllamaProvider(config=config).generate("hi")

    payload = route.calls.last.request.content
    assert b"system" in payload


@respx.mock
def test_generate_raises_on_http_error() -> None:
    config = OllamaConfig()
    respx.post(f"{config.base_url}/api/generate").mock(return_value=httpx.Response(500))

    with pytest.raises(httpx.HTTPStatusError):
        OllamaProvider(config=config).generate("hi")


@respx.mock
def test_generate_strips_a_completed_think_block() -> None:
    config = OllamaConfig()
    respx.post(f"{config.base_url}/api/generate").mock(
        return_value=httpx.Response(
            200,
            json={
                "response": "<think>\nreasoning about the reply\n</think>\n\nLeave it by the door."
            },
        )
    )

    result = OllamaProvider(config=config).generate("hi")

    assert result == "Leave it by the door."


@respx.mock
def test_generate_drops_an_unterminated_think_block() -> None:
    # A model cut off mid-reasoning (e.g. deepseek-r1 hitting PolicyEngine's
    # length cap before closing </think>) never produced a real answer --
    # the result should be empty, not the dangling reasoning fragment.
    config = OllamaConfig()
    respx.post(f"{config.base_url}/api/generate").mock(
        return_value=httpx.Response(
            200, json={"response": "<think>\nokay so I need to figure out how to"}
        )
    )

    result = OllamaProvider(config=config).generate("hi")

    assert result == ""
