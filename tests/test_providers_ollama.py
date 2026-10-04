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


_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "classify_intent",
            "description": "Record the best-matching category",
            "parameters": {"type": "object", "properties": {}},
        },
    }
]


@respx.mock
def test_chat_with_tools_parses_a_tool_call() -> None:
    config = OllamaConfig()
    respx.post(f"{config.base_url}/api/chat").mock(
        return_value=httpx.Response(
            200,
            json={
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "function": {
                                "name": "classify_intent",
                                "arguments": {"category": "food_delivery", "confidence": 0.9},
                            }
                        }
                    ],
                }
            },
        )
    )

    result = OllamaProvider(config=config).chat_with_tools(
        [{"role": "user", "content": "hi"}], _TOOLS
    )

    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "classify_intent"
    assert result.tool_calls[0].arguments == {"category": "food_delivery", "confidence": 0.9}
    assert result.content == ""


@respx.mock
def test_chat_with_tools_parses_multiple_tool_calls_in_order() -> None:
    config = OllamaConfig()
    respx.post(f"{config.base_url}/api/chat").mock(
        return_value=httpx.Response(
            200,
            json={
                "message": {
                    "content": "",
                    "tool_calls": [
                        {"function": {"name": "classify_intent", "arguments": {}}},
                        {"function": {"name": "flag_for_review", "arguments": {"summary": "x"}}},
                    ],
                }
            },
        )
    )

    result = OllamaProvider(config=config).chat_with_tools(
        [{"role": "user", "content": "hi"}], _TOOLS
    )

    assert [call.name for call in result.tool_calls] == ["classify_intent", "flag_for_review"]


@respx.mock
def test_chat_with_tools_returns_plain_content_when_model_ignores_tools() -> None:
    config = OllamaConfig()
    respx.post(f"{config.base_url}/api/chat").mock(
        return_value=httpx.Response(200, json={"message": {"content": "food_delivery"}})
    )

    result = OllamaProvider(config=config).chat_with_tools(
        [{"role": "user", "content": "hi"}], _TOOLS
    )

    assert result.tool_calls == []
    assert result.content == "food_delivery"


@respx.mock
def test_chat_with_tools_strips_thinking_from_content() -> None:
    config = OllamaConfig()
    respx.post(f"{config.base_url}/api/chat").mock(
        return_value=httpx.Response(
            200, json={"message": {"content": "<think>\nreasoning\n</think>\n\nfood_delivery"}}
        )
    )

    result = OllamaProvider(config=config).chat_with_tools(
        [{"role": "user", "content": "hi"}], _TOOLS
    )

    assert result.content == "food_delivery"


@respx.mock
def test_chat_with_tools_raises_on_http_error() -> None:
    config = OllamaConfig()
    respx.post(f"{config.base_url}/api/chat").mock(return_value=httpx.Response(500))

    with pytest.raises(httpx.HTTPStatusError):
        OllamaProvider(config=config).chat_with_tools([{"role": "user", "content": "hi"}], _TOOLS)


@respx.mock
def test_chat_with_tools_sends_messages_and_tools_in_the_request_body() -> None:
    config = OllamaConfig()
    route = respx.post(f"{config.base_url}/api/chat").mock(
        return_value=httpx.Response(200, json={"message": {"content": ""}})
    )

    OllamaProvider(config=config).chat_with_tools(
        [{"role": "user", "content": "a very specific visitor line"}], _TOOLS
    )

    sent_body = route.calls.last.request.content
    assert b"a very specific visitor line" in sent_body
    assert b"classify_intent" in sent_body
