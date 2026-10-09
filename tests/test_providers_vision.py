import base64
import io
import json

import httpx
import pytest
import respx
from PIL import Image

from knock.config import VisionConfig
from knock.core.scene import SceneObservation
from knock.providers.vision.base import observe_scene
from knock.providers.vision.ollama import OllamaVisionProvider
from knock.providers.vision.safety import SAFE_FALLBACK_DESCRIPTION


def _jpeg_bytes(width: int, height: int) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color=(100, 120, 140)).save(buffer, format="JPEG")
    return buffer.getvalue()


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


@respx.mock
def test_describe_downscales_a_large_image_before_sending() -> None:
    config = VisionConfig()
    route = respx.post(f"{config.base_url}/api/generate").mock(
        return_value=httpx.Response(200, json={"response": "ok"})
    )

    large = _jpeg_bytes(4032, 3024)
    OllamaVisionProvider(config=config).describe(large)

    sent_body = json.loads(route.calls.last.request.content)
    sent_image = base64.b64decode(sent_body["images"][0])
    with Image.open(io.BytesIO(sent_image)) as resized:
        assert max(resized.size) <= 1024
    assert len(sent_image) < len(large)


@respx.mock
def test_describe_leaves_a_small_image_unchanged() -> None:
    config = VisionConfig()
    route = respx.post(f"{config.base_url}/api/generate").mock(
        return_value=httpx.Response(200, json={"response": "ok"})
    )

    small = _jpeg_bytes(400, 300)
    OllamaVisionProvider(config=config).describe(small)

    sent_body = json.loads(route.calls.last.request.content)
    sent_image = base64.b64decode(sent_body["images"][0])
    assert sent_image == small


@respx.mock
def test_describe_sends_unparseable_image_bytes_unchanged() -> None:
    config = VisionConfig()
    route = respx.post(f"{config.base_url}/api/generate").mock(
        return_value=httpx.Response(200, json={"response": "ok"})
    )

    garbage = b"\x00\x01\x02not-a-real-image" * 100
    OllamaVisionProvider(config=config).describe(garbage)

    sent_body = json.loads(route.calls.last.request.content)
    assert base64.b64decode(sent_body["images"][0]) == garbage


# -- observe() / structured output ------------------------------------------------


def _observation_json(**overrides) -> str:
    data = {
        "people_count": 1,
        "carrying": ["box"],
        "package_visible": True,
        "uniform_or_logo": "UPS",
        "vehicle": "",
        "visible_text": "",
        "summary": "A person holding a box.",
    }
    data.update(overrides)
    return json.dumps(data)


@respx.mock
def test_observe_requests_structured_output_and_parses_it() -> None:
    config = VisionConfig()
    route = respx.post(f"{config.base_url}/api/generate").mock(
        return_value=httpx.Response(200, json={"response": _observation_json()})
    )

    observation = OllamaVisionProvider(config=config).observe(b"fake-jpeg-bytes")

    body = json.loads(route.calls.last.request.content)
    assert body["format"]["type"] == "object"
    assert "uniform_or_logo" in body["format"]["required"]
    assert body["images"]
    assert observation.people_count == 1
    assert observation.carrying == ["box"]
    assert observation.package_visible is True
    assert observation.uniform_or_logo == "UPS"
    # "" means "not visible" in the schema, None in the model.
    assert observation.vehicle is None
    assert observation.visible_text is None
    assert observation.summary == "A person holding a box."


@respx.mock
def test_observe_drops_alarming_fields_and_sanitizes_the_summary() -> None:
    config = VisionConfig()
    respx.post(f"{config.base_url}/api/generate").mock(
        return_value=httpx.Response(
            200,
            json={
                "response": _observation_json(
                    carrying=["box", "a knife"],
                    vehicle="a van with a gun rack",
                    summary="A person who might have a weapon.",
                )
            },
        )
    )

    observation = OllamaVisionProvider(config=config).observe(b"fake-jpeg-bytes")

    assert observation.carrying == ["box"]
    assert observation.vehicle is None
    assert observation.summary == SAFE_FALLBACK_DESCRIPTION


@respx.mock
def test_observe_ignores_wrongly_typed_fields() -> None:
    config = VisionConfig()
    respx.post(f"{config.base_url}/api/generate").mock(
        return_value=httpx.Response(
            200,
            json={
                "response": _observation_json(
                    people_count=True, package_visible="yes", carrying="box"
                )
            },
        )
    )

    observation = OllamaVisionProvider(config=config).observe(b"fake-jpeg-bytes")

    assert observation.people_count is None
    assert observation.package_visible is None
    assert observation.carrying == []


@respx.mock
def test_observe_falls_back_to_describe_on_unparseable_output() -> None:
    config = VisionConfig()
    route = respx.post(f"{config.base_url}/api/generate").mock(
        side_effect=[
            httpx.Response(200, json={"response": "not json at all"}),
            httpx.Response(200, json={"response": "A person at the door."}),
        ]
    )

    observation = OllamaVisionProvider(config=config).observe(b"fake-jpeg-bytes")

    assert route.call_count == 2
    assert "format" not in json.loads(route.calls.last.request.content)
    assert observation.summary == "A person at the door."
    assert observation.people_count is None


@respx.mock
def test_keep_alive_is_sent_only_when_configured() -> None:
    unset = VisionConfig()
    route = respx.post(f"{unset.base_url}/api/generate").mock(
        return_value=httpx.Response(200, json={"response": "ok"})
    )

    OllamaVisionProvider(config=unset).describe(b"img")
    assert "keep_alive" not in json.loads(route.calls.last.request.content)

    OllamaVisionProvider(config=VisionConfig(keep_alive="-1")).describe(b"img")
    # A bare number must go out as a JSON number -- Ollama rejects "-1" as a
    # duration string.
    assert json.loads(route.calls.last.request.content)["keep_alive"] == -1


@respx.mock
def test_warm_up_loads_the_model_with_an_empty_request() -> None:
    config = VisionConfig(keep_alive="24h")
    route = respx.post(f"{config.base_url}/api/generate").mock(
        return_value=httpx.Response(200, json={"done": True})
    )

    assert OllamaVisionProvider(config=config).warm_up() is True
    assert json.loads(route.calls.last.request.content) == {
        "model": config.model,
        "keep_alive": "24h",
    }


@respx.mock
def test_warm_up_failure_is_reported_not_raised() -> None:
    config = VisionConfig()
    respx.post(f"{config.base_url}/api/generate").mock(return_value=httpx.Response(500))

    assert OllamaVisionProvider(config=config).warm_up() is False


def test_observe_scene_uses_describe_for_a_describe_only_provider() -> None:
    class _DescribeOnly:
        name = "describe-only"

        def describe(self, image: bytes, prompt: str | None = None) -> str:
            return "A person at the door."

    observation = observe_scene(_DescribeOnly(), b"img")

    assert observation == SceneObservation(summary="A person at the door.")
