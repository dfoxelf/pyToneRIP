# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Raw calibration raster decoding.

Calibration targets bypass QUAD curves and colour management. The selected
printer mode's density, normal gamma and ink limit are applied later in the
shared raster pipeline. Targets are accepted only while calibration mode is
explicitly enabled in Preferences.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps


RGB_SELECTOR_TO_INK = {
    (127, 255): "K", (191, 255): "C", (223, 255): "M", (239, 255): "Y",
    (247, 255): "LC", (251, 255): "LM", (253, 255): "LK", (254, 255): "LLK",
    (255, 127): "O", (255, 191): "G",
}


@dataclass(frozen=True, slots=True)
class CalibrationRaster:
    width: int
    rows_by_ink: dict[str, tuple[bytes, ...]]
    dpi: tuple[float, float] | None

    @property
    def height(self) -> int:
        return len(next(iter(self.rows_by_ink.values())))


def _open(path: Path) -> Image.Image:
    image = Image.open(path)
    if image.format != "TIFF" or getattr(image, "n_frames", 1) != 1:
        image.close()
        raise ValueError("Calibration mode requires a single-page TIFF")
    return image


def _dpi(image: Image.Image) -> tuple[float, float] | None:
    value = image.info.get("dpi")
    return (float(value[0]), float(value[1])) if isinstance(value, tuple) and len(value) == 2 else None


def decode_rgb_selectors(path: Path, inks: tuple[str, ...], ink_limit_percent: int) -> CalibrationRaster:
    """Decode QTR's exact RGB selector convention into raw per-ink codes."""

    if not 0 <= ink_limit_percent <= 100:
        raise ValueError("calibration ink limit must be in range 0..100")
    with _open(path) as image:
        rgb = ImageOps.exif_transpose(image).convert("RGB")
        width, height = rgb.size
        result = {ink: [] for ink in inks}
        selected = set(inks)
        pixels = rgb.load()
        buffers = {ink: bytearray(width * height) for ink in inks}
        for y in range(height):
            for x in range(width):
                r, g, b = pixels[x, y]
                ink = RGB_SELECTOR_TO_INK.get((r, b))
                if ink == "K" and ink not in selected:
                    # PK/MK curves are mutually exclusive logical alternatives
                    # for one native black selector on interchangeable-black
                    # printers. The standard RGB target still identifies that
                    # row as K; route it to whichever logical black is active.
                    ink = next((name for name in ("PK", "MK") if name in selected), ink)
                if ink in selected:
                    buffers[ink][y * width + x] = (255 - g) * ink_limit_percent // 100
        for ink, data in buffers.items():
            result[ink] = tuple(bytes(data[y * width:(y + 1) * width]) for y in range(height))
        return CalibrationRaster(width, result, _dpi(image))


def decode_per_channel(path: Path, inks: tuple[str, ...], ink_limit_percent: int) -> CalibrationRaster:
    """Assign equal target rows, top to bottom, to successive logical inks.

    The bundled per-channel target has one horizontal target row per ink.  A
    white margin may precede it, so the active span is detected rather than
    assumed to start at pixel zero.  This intentionally does not try to
    interpret arbitrary photographs as calibration targets.
    """

    if not 0 <= ink_limit_percent <= 100:
        raise ValueError("calibration ink limit must be in range 0..100")
    with _open(path) as image:
        gray = ImageOps.exif_transpose(image).convert("L")
        width, height = gray.size
        source = gray.tobytes()
        active_rows = [y for y in range(height) if any(value < 250 for value in source[y * width:(y + 1) * width])]
        if not active_rows:
            raise ValueError("per-channel calibration TIFF contains no target rows")
        first, last = active_rows[0], active_rows[-1] + 1
        span = last - first
        if span < len(inks):
            raise ValueError("per-channel calibration TIFF is too short for selected inks")
        buffers = {ink: bytearray(width * height) for ink in inks}
        for index, ink in enumerate(inks):
            start = first + span * index // len(inks)
            end = first + span * (index + 1) // len(inks)
            for y in range(start, end):
                row = source[y * width:(y + 1) * width]
                buffers[ink][y * width:(y + 1) * width] = bytes((255 - value) * ink_limit_percent // 100 for value in row)
        rows = {ink: tuple(bytes(data[y * width:(y + 1) * width]) for y in range(height)) for ink, data in buffers.items()}
        return CalibrationRaster(width, rows, _dpi(image))
