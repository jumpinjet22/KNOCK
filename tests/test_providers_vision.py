import base64

import httpx
import pytest
import respx

from knock.config import VisionConfig
from knock.providers.vision.ollama import OllamaVisionProvider
from knock.providers.vision.safety import SAFE_FALLBACK_DESCRIPTION


@respx.mock
def test_describe_returns_trimmed_response_text() -> None:
    config = VisionConfig(host="127.0.0.1", port=11434, model="moondream")
    route = respx.post(f"{config.base_url}/api/generate").mock(
        return_value=httpx.Response(200, json={"response": "  A person with a box.  "})
    )

    provider = OllamaVisionProvider(config=config)
    result = provider.describe(b"fake-jpeg-bytes")

    assert result == "A person with a box."
    assert route.called


@respx.mock
def test_describe_sends_base64_encoded_image() -> None:
    config = VisionConfig()
    route = respx.post(f"{config.base_url}/api/generate").mock(
        return_value=httpx.Response(200, json={"response": "ok"})
    )

    image = b"\x00\x01\x02binary-image-data"
    OllamaVisionProvider(config=config).describe(image)

    sent_body = route.calls.last.request.content
    expected_b64 = base64.b64encode(image).decode("ascii")
    assert expected_b64.encode() in sent_body
    assert b"moondream" in sent_body


@respx.mock
def test_describe_uses_default_prompt_unless_overridden() -> None:
    config = VisionConfig()
    route = respx.post(f"{config.base_url}/api/generate").mock(
        return_value=httpx.Response(200, json={"response": "ok"})
    )

    OllamaVisionProvider(config=config).describe(b"img")
    assert b"package" in route.calls.last.request.content

    OllamaVisionProvider(config=config).describe(b"img", prompt="Custom prompt")
    assert b"Custom prompt" in route.calls.last.request.content


@respx.mock
def test_describe_raises_on_http_error() -> None:
    config = VisionConfig()
    respx.post(f"{config.base_url}/api/generate").mock(return_value=httpx.Response(500))

    with pytest.raises(httpx.HTTPStatusError):
        OllamaVisionProvider(config=config).describe(b"img")


@respx.mock
def test_describe_sanitizes_alarming_model_output() -> None:
    config = VisionConfig()
    respx.post(f"{config.base_url}/api/generate").mock(
        return_value=httpx.Response(200, json={"response": "A person holding what might be a bomb"})
    )

    result = OllamaVisionProvider(config=config).describe(b"img")

    assert result == SAFE_FALLBACK_DESCRIPTION


@respx.mock
def test_describe_raw_does_not_sanitize_alarming_model_output() -> None:
    config = VisionConfig()
    respx.post(f"{config.base_url}/api/generate").mock(
        return_value=httpx.Response(200, json={"response": "A person holding what might be a bomb"})
    )

    result = OllamaVisionProvider(config=config).describe_raw(b"img")

    assert result == "A person holding what might be a bomb"
