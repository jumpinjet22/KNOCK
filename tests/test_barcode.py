import io
from typing import Any, cast

import zxingcpp
from PIL import Image

from knock.providers.vision.barcode import decode_tracking_barcode


def _barcode_jpeg(text: str, fmt: zxingcpp.BarcodeFormat | None = None) -> bytes:
    barcode = zxingcpp.create_barcode(text, fmt or zxingcpp.BarcodeFormat.Code128)
    # zxingcpp.Image satisfies PIL's array-interface duck type at runtime
    # (confirmed live); cast rather than `type: ignore` since whether mypy
    # considers the raw call an error at all is version-dependent (seen
    # live: needed locally, flagged as unused on CI's resolved versions).
    img = Image.fromarray(cast(Any, barcode.to_image(scale=5)))
    buffer = io.BytesIO()
    img.convert("RGB").save(buffer, format="JPEG", quality=95)
    return buffer.getvalue()


def test_decodes_a_real_ups_tracking_barcode() -> None:
    tracking_number = "1Z16E2K90300002616"
    result = decode_tracking_barcode(_barcode_jpeg(tracking_number))

    assert result == ("UPS", tracking_number)


def test_decodes_a_real_amazon_logistics_barcode() -> None:
    tracking_number = "TBA123456789012"
    result = decode_tracking_barcode(_barcode_jpeg(tracking_number))

    assert result == ("Amazon Logistics", tracking_number)


def test_decodes_a_real_usps_numeric_barcode() -> None:
    tracking_number = "9400111899223197428490"
    result = decode_tracking_barcode(_barcode_jpeg(tracking_number))

    assert result == ("USPS", tracking_number)


def test_ignores_a_barcode_that_doesnt_match_any_known_carrier_format() -> None:
    # Confirmed live: a real package label's sort/routing code (shaped
    # like this) is also a real, decodable Code128 barcode -- it must
    # never be mistaken for a tracking number.
    result = decode_tracking_barcode(_barcode_jpeg("TX7756-01"))

    assert result is None


def test_returns_none_for_unparseable_image_bytes() -> None:
    assert decode_tracking_barcode(b"not an image") is None


def test_returns_none_when_no_barcode_is_present() -> None:
    plain = Image.new("RGB", (200, 200), color=(100, 120, 140))
    buffer = io.BytesIO()
    plain.save(buffer, format="JPEG")

    assert decode_tracking_barcode(buffer.getvalue()) is None
