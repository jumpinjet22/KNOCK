#!/usr/bin/env python3
"""Manual smoke test against real local Ollama / Whisper / Kokoro services.

Not part of the automated test suite (no mocking here on purpose) -- run this
by hand after starting your local Ollama, Wyoming-Whisper, and Wyoming-Kokoro
services to sanity-check connectivity and the basic request/response shape.

Usage:
    python scripts/smoke_test_providers.py [--skip llm,stt,tts,vision]

Configuration is read from the same
KNOCK_OLLAMA_*/KNOCK_WHISPER_*/KNOCK_KOKORO_*/KNOCK_VISION_* environment
variables the real providers use (see knock.config), falling back to each
provider's localhost defaults.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import math
import struct
import sys

from knock.config import KokoroConfig, OllamaConfig, VisionConfig, WhisperConfig
from knock.providers.llm.ollama import OllamaProvider
from knock.providers.stt.whisper import WhisperSTTProvider
from knock.providers.tts.kokoro import KokoroTTSProvider
from knock.providers.vision.ollama import OllamaVisionProvider

# A 1x1 transparent PNG -- just enough of a real image to exercise the wire
# format; don't expect a meaningful description back from it.
_TEST_IMAGE_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _silence(seconds: float = 1.0, rate: int = 16000) -> bytes:
    """A short burst of near-silent PCM audio, just to exercise the wire format."""
    samples = int(seconds * rate)
    return struct.pack(f"<{samples}h", *([0] * samples))


def _tone(seconds: float = 1.0, rate: int = 16000, hz: float = 440.0) -> bytes:
    samples = int(seconds * rate)
    return struct.pack(
        f"<{samples}h",
        *(int(3000 * math.sin(2 * math.pi * hz * i / rate)) for i in range(samples)),
    )


def check_llm() -> None:
    print(f"--- Ollama ({OllamaConfig.from_env().base_url}) ---")
    provider = OllamaProvider(config=OllamaConfig.from_env())
    try:
        reply = provider.generate("Say hello in five words or fewer.")
        print(f"response: {reply!r}")
    finally:
        provider.close()


def check_stt() -> None:
    config = WhisperConfig.from_env()
    print(f"--- Whisper (Wyoming @ {config.host}:{config.port}) ---")
    provider = WhisperSTTProvider(config=config)
    text = asyncio.run(provider.transcribe(_tone(), rate=16000, width=2, channels=1))
    print(f"transcript of a 440Hz test tone: {text!r}")


def check_tts() -> None:
    config = KokoroConfig.from_env()
    print(f"--- Kokoro (Wyoming @ {config.host}:{config.port}) ---")
    provider = KokoroTTSProvider(config=config)
    result = asyncio.run(provider.synthesize("Hello, this is a test."))
    print(f"synthesized {len(result.audio)} bytes @ {result.rate}Hz/{result.width * 8}bit")


def check_vision() -> None:
    config = VisionConfig.from_env()
    print(f"--- Vision ({config.base_url}, model={config.model}) ---")
    provider = OllamaVisionProvider(config=config)
    try:
        description = provider.describe(_TEST_IMAGE_PNG)
        print(f"description of a 1x1 test image: {description!r}")
    finally:
        provider.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip",
        default="",
        help="comma-separated subset of llm,stt,tts,vision to skip",
    )
    args = parser.parse_args()
    skip = {name.strip() for name in args.skip.split(",") if name.strip()}

    checks = {"llm": check_llm, "stt": check_stt, "tts": check_tts, "vision": check_vision}
    failures = []
    for name, check in checks.items():
        if name in skip:
            print(f"(skipping {name})")
            continue
        try:
            check()
        except Exception as exc:  # noqa: BLE001 - this is a manual diagnostic script
            failures.append(name)
            print(f"FAILED: {name}: {exc}", file=sys.stderr)

    if failures:
        print(f"\n{len(failures)} provider(s) failed: {', '.join(failures)}", file=sys.stderr)
        return 1

    print("\nAll checked providers responded.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
