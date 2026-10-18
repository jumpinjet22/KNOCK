"""Deterministic barcode decoding for a package-camera photo's tracking
number -- a different tool for a different job than the vision-LLM read in
ollama.py. Confirmed live: a vision model asked to read small printed text
mistook a package's sort/routing code for its tracking number, and
conflated the sender's return address (Amazon) with the actual carrier
printed on the label (UPS Ground). A barcode is either decoded exactly or
not decoded at all -- no character-level guessing -- and a tracking
number's own format is often carrier-specific enough to identify the
carrier with more confidence than reading a logo/address ever can.

Deliberately narrow: only patterns distinctive enough to be confident about
(UPS's `1Z` prefix, Amazon Logistics' `TBA` prefix, USPS's two known
formats) are recognized. FedEx and DHL's tracking numbers are plain
numeric strings with no distinctive prefix -- a label often carries
several unrelated numeric barcodes (SKUs, sort codes), so a bare
digit-count match risks confidently returning the WRONG number, the exact
mistake this module exists to fix. Left as a documented gap, not guessed.
"""

from __future__ import annotations

import io
import logging
import re

import zxingcpp
from PIL import Image, UnidentifiedImageError

logger = logging.getLogger(__name__)

_TRACKING_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("UPS", re.compile(r"^1Z[0-9A-Z]{16}$")),
    ("USPS", re.compile(r"^\d{20,22}$")),
    ("USPS", re.compile(r"^[A-Z]{2}\d{9}US$")),
    ("Amazon Logistics", re.compile(r"^TBA\d{12}$")),
)


def decode_tracking_barcode(image: bytes) -> tuple[str, str] | None:
    """(carrier, tracking_number) from the first decoded barcode whose text
    confidently matches a known carrier format, or None -- no barcode
    found, none parseable, or none match a recognized format confidently.

    Scans the full, original (never downscaled) snapshot directly, not a
    cropped region -- unlike the LLM path, a barcode benefits from maximum
    bar-width resolution, and zxing-cpp already finds/isolates barcode
    regions within a larger image on its own, so no localization step is
    needed here.

    Best-effort: any failure (unparseable image, decode error) is logged
    and returns None, never raises -- this is enrichment, never a reason
    to block the rest of the response.
    """
    try:
        with Image.open(io.BytesIO(image)) as img:
            results = zxingcpp.read_barcodes(img)
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        logger.info("Barcode decode failed: %s", exc)
        return None
    for result in results:
        text = result.text.strip().replace(" ", "")
        for carrier, pattern in _TRACKING_PATTERNS:
            if pattern.match(text):
                return carrier, text
    return None
