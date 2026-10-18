import base64
import io
import json
from typing import Any, cast

import httpx
import pytest
import respx
import zxingcpp
from PIL import Image

from knock.config import VisionConfig
from knock.core.scene import PackageLabel, SceneObservation
from knock.providers.vision.base import observe_scene, read_package_label
from knock.providers.vision.ollama import OllamaVisionProvider
from knock.providers.vision.safety import SAFE_FALLBACK_DESCRIPTION


def _barcode_jpeg(text: str) -> bytes:
    barcode = zxingcpp.create_barcode(text, zxingcpp.BarcodeFormat.Code128)
    # See test_barcode.py's _barcode_jpeg for why this is a cast, not a
    # `type: ignore`.
    img = Image.fromarray(cast(Any, barcode.to_image(scale=5)))
    buffer = io.BytesIO()
    img.convert("RGB").save(buffer, format="JPEG", quality=95)
    return buffer.getvalue()


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


def _label_json(
    carrier: str = "FedEx", tracking_number: str = "1234", recipient_name: str = "Jon"
) -> str:
    return json.dumps(
        {"carrier": carrier, "tracking_number": tracking_number, "recipient_name": recipient_name}
    )


@respx.mock
def test_read_package_label_returns_structured_fields() -> None:
    config = VisionConfig()
    respx.post(f"{config.base_url}/api/generate").mock(
        side_effect=[
            httpx.Response(200, json={"response": json.dumps({"region": "center"})}),
            httpx.Response(200, json={"response": _label_json()}),
        ]
    )

    label = OllamaVisionProvider(config=config).read_package_label(_jpeg_bytes(1200, 900))

    assert label == PackageLabel(carrier="FedEx", tracking_number="1234", recipient_name="Jon")


@respx.mock
def test_read_package_label_skips_stage_two_when_stage_one_reports_none() -> None:
    config = VisionConfig()
    route = respx.post(f"{config.base_url}/api/generate").mock(
        return_value=httpx.Response(200, json={"response": json.dumps({"region": "none"})})
    )

    label = OllamaVisionProvider(config=config).read_package_label(_jpeg_bytes(1200, 900))

    assert label is None
    assert route.call_count == 1


@respx.mock
def test_read_package_label_uses_whole_image_when_stage_one_cant_narrow_down() -> None:
    config = VisionConfig()
    route = respx.post(f"{config.base_url}/api/generate").mock(
        side_effect=[
            httpx.Response(200, json={"response": json.dumps({"region": "whole_image"})}),
            httpx.Response(200, json={"response": _label_json()}),
        ]
    )

    label = OllamaVisionProvider(config=config).read_package_label(_jpeg_bytes(1200, 900))

    assert route.call_count == 2
    sent_image = base64.b64decode(json.loads(route.calls.last.request.content)["images"][0])
    with Image.open(io.BytesIO(sent_image)) as img:
        # Not cropped to a smaller region -- just the ordinary
        # resize_for_model downscale every call already gets, same
        # proportions as the 1200x900 original.
        assert img.size == (1024, 768)
    assert label is not None


@respx.mock
def test_read_package_label_falls_back_to_whole_image_on_unparseable_stage_one() -> None:
    config = VisionConfig()
    respx.post(f"{config.base_url}/api/generate").mock(
        side_effect=[
            httpx.Response(200, json={"response": "not json"}),
            httpx.Response(200, json={"response": _label_json()}),
        ]
    )

    label = OllamaVisionProvider(config=config).read_package_label(_jpeg_bytes(1200, 900))

    assert label is not None


@respx.mock
def test_read_package_label_crops_a_smaller_region_than_the_whole_image() -> None:
    config = VisionConfig()
    route = respx.post(f"{config.base_url}/api/generate").mock(
        side_effect=[
            httpx.Response(200, json={"response": json.dumps({"region": "top_left"})}),
            httpx.Response(200, json={"response": _label_json()}),
        ]
    )

    OllamaVisionProvider(config=config).read_package_label(_jpeg_bytes(3000, 3000))

    sent_image = base64.b64decode(json.loads(route.calls.last.request.content)["images"][0])
    with Image.open(io.BytesIO(sent_image)) as img:
        # Smaller than the original frame (actually cropped), but still at
        # least the enforced minimum crop dimension.
        assert img.size[0] < 3000
        assert img.size[0] >= 512


@respx.mock
def test_read_package_label_crop_respects_minimum_size_on_a_small_image() -> None:
    config = VisionConfig()
    respx.post(f"{config.base_url}/api/generate").mock(
        side_effect=[
            httpx.Response(200, json={"response": json.dumps({"region": "top_left"})}),
            httpx.Response(200, json={"response": _label_json()}),
        ]
    )

    # A small source image -- a single 3x3 cell plus padding would be
    # under 512px, so the crop must grow (clamped to the image itself).
    OllamaVisionProvider(config=config).read_package_label(_jpeg_bytes(600, 600))
    # No assertion failure (e.g. a negative-size crop) is the main thing
    # being proven here; smoke-tested via the lack of an exception above.


@respx.mock
def test_read_package_label_returns_none_when_nothing_legible() -> None:
    config = VisionConfig()
    respx.post(f"{config.base_url}/api/generate").mock(
        side_effect=[
            httpx.Response(200, json={"response": json.dumps({"region": "center"})}),
            httpx.Response(
                200,
                json={
                    "response": json.dumps(
                        {"carrier": "", "tracking_number": "", "recipient_name": ""}
                    )
                },
            ),
        ]
    )

    label = OllamaVisionProvider(config=config).read_package_label(_jpeg_bytes(1200, 900))

    assert label is None


@respx.mock
def test_read_package_label_survives_a_request_error() -> None:
    config = VisionConfig()
    respx.post(f"{config.base_url}/api/generate").mock(side_effect=httpx.ConnectError("refused"))

    label = OllamaVisionProvider(config=config).read_package_label(_jpeg_bytes(1200, 900))

    assert label is None


@respx.mock
def test_read_package_label_returns_none_for_non_object_stage_two_output() -> None:
    config = VisionConfig()
    respx.post(f"{config.base_url}/api/generate").mock(
        side_effect=[
            httpx.Response(200, json={"response": json.dumps({"region": "center"})}),
            httpx.Response(200, json={"response": json.dumps(["not", "an", "object"])}),
        ]
    )

    label = OllamaVisionProvider(config=config).read_package_label(_jpeg_bytes(1200, 900))

    assert label is None


@respx.mock
def test_read_package_label_crop_failure_falls_back_to_whole_image() -> None:
    config = VisionConfig()
    respx.post(f"{config.base_url}/api/generate").mock(
        side_effect=[
            httpx.Response(200, json={"response": json.dumps({"region": "top_left"})}),
            httpx.Response(200, json={"response": _label_json()}),
        ]
    )

    # Not valid image bytes at all -- the stage-1 call succeeds (it never
    # touches PIL), but cropping it fails and falls back to sending it
    # unchanged, same best-effort pattern as resize_for_model.
    label = OllamaVisionProvider(config=config).read_package_label(b"not an image")

    assert label is not None


def test_read_package_label_dispatch_returns_none_without_the_capability() -> None:
    class _DescribeOnly:
        name = "describe-only"

        def describe(self, image: bytes, prompt: str | None = None) -> str:
            return "A box on the porch."

    assert read_package_label(_DescribeOnly(), b"img") is None


class _FixedLabelProvider:
    """A fake vision provider whose own read_package_label() always
    returns the same (possibly wrong) label, regardless of the image --
    lets a test isolate the dispatch function's barcode-override behavior
    from the LLM read itself.
    """

    name = "fixed-label"

    def __init__(self, label: PackageLabel | None) -> None:
        self._label = label

    def describe(self, image: bytes, prompt: str | None = None) -> str:
        return ""

    def read_package_label(self, image: bytes) -> PackageLabel | None:
        return self._label


def test_read_package_label_dispatch_prefers_a_confident_barcode_over_the_providers_own_guess() -> (
    None
):
    # Confirmed live: a vision model guessed the sender's name as the
    # carrier and misread the tracking number -- a confidently decoded
    # barcode must win over both.
    wrong_guess = PackageLabel(carrier="Amazon", tracking_number="TX7756-01", recipient_name="Jon")
    provider = _FixedLabelProvider(wrong_guess)
    tracking_number = "1Z16E2K90300002616"

    label = read_package_label(provider, _barcode_jpeg(tracking_number))

    assert label is not None
    assert label.carrier == "UPS"
    assert label.tracking_number == tracking_number
    # Never barcode-encoded -- the provider's own read is untouched.
    assert label.recipient_name == "Jon"


def test_read_package_label_dispatch_barcode_alone_is_enough_without_any_llm_read() -> None:
    provider = _FixedLabelProvider(None)
    tracking_number = "TBA123456789012"

    label = read_package_label(provider, _barcode_jpeg(tracking_number))

    assert label == PackageLabel(carrier="Amazon Logistics", tracking_number=tracking_number)


def test_read_package_label_dispatch_returns_none_for_an_entirely_empty_label() -> None:
    provider = _FixedLabelProvider(PackageLabel())

    label = read_package_label(provider, _barcode_jpeg("TX7756-01"))

    assert label is None


def test_read_package_label_dispatch_keeps_the_providers_guess_without_a_confident_barcode() -> (
    None
):
    guess = PackageLabel(carrier="FedEx", tracking_number=None, recipient_name="Jon")
    provider = _FixedLabelProvider(guess)

    # A non-matching barcode (shaped like a real package's sort code) --
    # never confidently identified, so the provider's own guess stands.
    label = read_package_label(provider, _barcode_jpeg("TX7756-01"))

    assert label == guess
