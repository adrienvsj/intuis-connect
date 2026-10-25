"""Brand images served by Home Assistant from the integration's brand folder."""
from __future__ import annotations

import struct
from pathlib import Path

from homeassistant.components.brands.const import ALLOWED_IMAGES

BRAND_DIR = Path(__file__).parents[2] / "custom_components" / "intuis_connect" / "brand"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _png_size(path: Path) -> tuple[int, int]:
    """Return (width, height) from the PNG header."""
    header = path.read_bytes()[:24]
    assert header[:8] == PNG_SIGNATURE, f"{path.name} is not a PNG"
    return struct.unpack(">II", header[16:24])


def test_brand_folder_only_holds_images_home_assistant_serves():
    names = {path.name for path in BRAND_DIR.iterdir()}
    assert {"icon.png", "icon@2x.png", "logo.png", "logo@2x.png"} <= names
    assert names <= ALLOWED_IMAGES


def test_brand_image_sizes_follow_the_brands_rules():
    for path in BRAND_DIR.iterdir():
        width, height = _png_size(path)
        scale = 2 if "@2x" in path.name else 1
        if "icon" in path.name:
            assert (width, height) == (256 * scale, 256 * scale), path.name
        else:
            # Logos keep their aspect ratio; the shortest side sets the size
            assert 128 * scale <= min(width, height) <= 256 * scale, path.name
