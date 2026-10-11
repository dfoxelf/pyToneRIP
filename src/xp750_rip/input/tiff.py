# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Strict Phase 7 decoder for validated 8-bit BlackIsZero grayscale TIFF."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from PIL import Image

if TYPE_CHECKING:
    from xp750_rip.performance import PerformanceTrace


class UnsupportedTiffError(ValueError):
    """Input requires TIFF semantics intentionally deferred beyond Phase 7."""


@dataclass(frozen=True, slots=True)
class Grayscale8Raster:
    width: int
    rows: tuple[bytes, ...]

    def __post_init__(self) -> None:
        if self.width <= 0 or not self.rows:
            raise ValueError("grayscale raster dimensions must be positive")
        if any(len(row) != self.width for row in self.rows):
            raise ValueError("every grayscale row must match the declared width")

    @property
    def height(self) -> int:
        return len(self.rows)

    def decimate(self, factor: int) -> Grayscale8Raster:
        """Take every Nth source pixel in both axes with no interpolation."""

        if isinstance(factor, bool) or not isinstance(factor, int) or factor <= 0:
            raise ValueError("integer decimation factor must be positive")
        rows = tuple(bytes(row[::factor]) for row in self.rows[::factor])
        return Grayscale8Raster(width=len(rows[0]), rows=rows)


@dataclass(frozen=True, slots=True)
class DecodedGrayscaleTiff:
    source_path: Path
    raster: Grayscale8Raster
    bits_per_sample: int
    photometric_interpretation: int
    orientation: int
    compression: int
    dpi: tuple[float, float] | None
    has_icc_profile: bool


def _single_tag_int(value: object, name: str) -> int:
    if isinstance(value, tuple):
        if len(value) != 1:
            raise UnsupportedTiffError(f"{name} must contain exactly one value")
        value = value[0]
    if not isinstance(value, int):
        raise UnsupportedTiffError(f"{name} is missing or not an integer")
    return value


def decode_grayscale_tiff(
    path: Path, *, trace: PerformanceTrace | None = None
) -> DecodedGrayscaleTiff:
    """Decode only the explicitly proven Phase 7 TIFF representation."""

    if trace is None:
        image = Image.open(path)
    else:
        with trace.stage("TIFF file open", path=path):
            image = Image.open(path)
    with image:
        if image.format != "TIFF":
            raise UnsupportedTiffError("input must be a TIFF file")
        if image.mode != "L":
            raise UnsupportedTiffError(
                "Phase 7 supports only single-channel 8-bit grayscale TIFF (Pillow mode L)"
            )
        bits = _single_tag_int(image.tag_v2.get(258), "BitsPerSample")
        photometric = _single_tag_int(
            image.tag_v2.get(262), "PhotometricInterpretation"
        )
        orientation = _single_tag_int(image.tag_v2.get(274, 1), "Orientation")
        compression = _single_tag_int(image.tag_v2.get(259, 1), "Compression")
        if bits != 8:
            raise UnsupportedTiffError(
                "Phase 7 supports only single-channel 8-bit grayscale TIFF (Pillow mode L)"
            )
        if photometric != 1:
            raise UnsupportedTiffError(
                "Phase 7 supports only TIFF PhotometricInterpretation=1 (BlackIsZero)"
            )
        if orientation != 1:
            raise UnsupportedTiffError("Phase 7 supports only TIFF Orientation=1")
        if compression not in {1, 5}:
            raise UnsupportedTiffError("Phase 7 supports only uncompressed or LZW TIFF")
        if trace is None:
            has_icc = bool(image.info.get("icc_profile"))
        else:
            with trace.stage("ICC metadata handling"):
                has_icc = bool(image.info.get("icc_profile"))
        # The monochrome QTR path intentionally ignores embedded ICC for
        # color conversion; the presence is retained as metadata only.
        width, height = image.size
        if trace is None:
            decoded = image.tobytes()
            rows = tuple(decoded[y * width : (y + 1) * width] for y in range(height))
        else:
            trace.event(
                "TIFF source DATA",
                width=width,
                height=height,
                pixels=width * height,
            )
            with trace.stage("TIFF decode"):
                decoded = image.tobytes()
                rows = tuple(decoded[y * width : (y + 1) * width] for y in range(height))
        dpi_value = image.info.get("dpi")
        dpi = (
            (float(dpi_value[0]), float(dpi_value[1]))
            if isinstance(dpi_value, tuple) and len(dpi_value) == 2
            else None
        )
    return DecodedGrayscaleTiff(
        source_path=path,
        raster=Grayscale8Raster(width, rows),
        bits_per_sample=bits,
        photometric_interpretation=photometric,
        orientation=orientation,
        compression=compression,
        dpi=dpi,
        has_icc_profile=has_icc,
    )


def tiff_black_is_zero_to_qtr_code(sample: int) -> int:
    """Convert TIFF 0=black/255=white to QTR 0=white/255=black."""

    if isinstance(sample, bool) or not isinstance(sample, int) or not 0 <= sample <= 255:
        raise ValueError("TIFF grayscale sample must be an integer in 0..255")
    return 255 - sample
