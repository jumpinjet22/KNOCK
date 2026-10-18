#!/usr/bin/env python3
"""Manual smoke test for the two-stage package-label read
(OllamaVisionProvider.read_package_label) against real local services.

Lists real UniFi Protect cameras (api_key-only, public API), fetches each
camera's current main + package-camera snapshot, and runs the real two-stage
read against the real configured vision model. Also runs the same read
against a locally generated synthetic label (a known, drawn-in-code ground
truth), since whatever's actually in frame on a real porch at the moment this
runs is unknown -- the synthetic check is what actually proves the crop/read
logic itself works, independent of whether a real package happens to be
sitting there right now.

Does NOT subscribe to Protect's event websocket, and never touches
STT/TTS/talkback -- every call here is a single one-shot REST request
(list cameras, fetch a snapshot, call Ollama). Nothing live, nothing that
speaks out loud. Safe to run any time, including overnight, without
disturbing the doorbell's normal operation.

Not part of the automated test suite -- a manual, hands-on dev tool, like
scripts/smoke_test_providers.py.

Usage:
    python scripts/smoke_test_package_label.py [--device-id ID] [--save-dir DIR]
"""

from __future__ import annotations

import argparse
import asyncio
import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from uiprotect import ProtectApiClient

from knock.config import UnifiConfig, VisionConfig
from knock.core.scene import PackageLabel
from knock.providers.vision.ollama import OllamaVisionProvider


def _synthetic_label_photo() -> tuple[bytes, PackageLabel]:
    """A large neutral "porch" canvas with a small, off-center white label
    drawn into it -- a known ground truth to check the two-stage read
    against, regardless of what's actually on a real porch right now.
    """
    carrier, tracking_number, recipient_name = "FedEx", "799123456784", "Jon Smith"
    canvas = Image.new("RGB", (2400, 1800), color=(90, 95, 100))
    draw = ImageDraw.Draw(canvas)
    label_box = (1600, 1150, 2100, 1500)
    draw.rectangle(label_box, fill=(245, 245, 245))
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont
    font_small: ImageFont.FreeTypeFont | ImageFont.ImageFont
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 34)
        font_small = ImageFont.truetype("DejaVuSans.ttf", 26)
    except OSError:
        font = font_small = ImageFont.load_default()
    draw.text((1620, 1170), carrier, fill="black", font=font)
    draw.text((1620, 1230), f"TRACKING {tracking_number}", fill="black", font=font_small)
    draw.text((1620, 1280), recipient_name, fill="black", font=font_small)
    buffer = io.BytesIO()
    canvas.save(buffer, format="JPEG", quality=90)
    expected = PackageLabel(
        carrier=carrier, tracking_number=tracking_number, recipient_name=recipient_name
    )
    return buffer.getvalue(), expected


def _print_label_result(name: str, label: PackageLabel | None) -> None:
    if label is None:
        print(f"  {name}: no label read (None)")
        return
    print(
        f"  {name}: carrier={label.carrier!r} "
        f"tracking_number={label.tracking_number!r} recipient_name={label.recipient_name!r}"
    )


async def _check_real_cameras(
    vision: OllamaVisionProvider, device_id: str | None, save_dir: Path | None
) -> None:
    unifi_config = UnifiConfig.from_env()
    print(f"--- UniFi Protect ({unifi_config.host}:{unifi_config.port}) ---")
    client = ProtectApiClient(
        host=unifi_config.host,
        port=unifi_config.port,
        api_key=unifi_config.api_key.get_secret_value(),
        verify_ssl=unifi_config.verify_ssl,
    )
    try:
        cameras = await client.get_cameras_public()
        if device_id is not None:
            cameras = [c for c in cameras if c.id == device_id]
        if not cameras:
            print("  no matching camera found")
            return
        for camera in cameras:
            print(f"  camera: {camera.name!r} ({camera.id}) reachable={camera.is_reachable}")
            try:
                snapshot = await client.get_public_api_camera_snapshot(camera.id, package=True)
            except Exception as exc:  # noqa: BLE001 - this is a manual diagnostic script
                print(f"    package-camera snapshot failed: {exc}")
                continue
            if snapshot is None:
                print("    no package-camera snapshot available (no package lens, or none ready)")
                continue
            if save_dir is not None:
                path = save_dir / f"{camera.id}_package.jpg"
                path.write_bytes(snapshot)
                print(f"    saved snapshot to {path}")
            label = await asyncio.to_thread(vision.read_package_label, snapshot)
            _print_label_result(f"{camera.name} (live)", label)
    finally:
        await client.close_session()


def _check_synthetic(vision: OllamaVisionProvider, save_dir: Path | None) -> None:
    print("--- Synthetic ground-truth label ---")
    photo, expected = _synthetic_label_photo()
    if save_dir is not None:
        path = save_dir / "synthetic_label.jpg"
        path.write_bytes(photo)
        print(f"  saved synthetic test photo to {path}")
    print(
        f"  expected: carrier={expected.carrier!r} "
        f"tracking_number={expected.tracking_number!r} recipient_name={expected.recipient_name!r}"
    )
    label = vision.read_package_label(photo)
    _print_label_result("synthetic (actual)", label)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--device-id", default=None, help="only check this one UniFi camera")
    parser.add_argument(
        "--save-dir",
        type=Path,
        default=None,
        help="save fetched/generated photos here for inspection",
    )
    parser.add_argument(
        "--skip-live", action="store_true", help="skip real UniFi cameras, synthetic check only"
    )
    args = parser.parse_args()
    if args.save_dir is not None:
        args.save_dir.mkdir(parents=True, exist_ok=True)

    vision_config = VisionConfig.from_env()
    print(f"Vision model: {vision_config.base_url}, model={vision_config.model}\n")
    vision = OllamaVisionProvider(config=vision_config)
    try:
        _check_synthetic(vision, args.save_dir)
        print()
        if not args.skip_live:
            asyncio.run(_check_real_cameras(vision, args.device_id, args.save_dir))
    finally:
        vision.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
