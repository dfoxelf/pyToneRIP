# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Validated TIFF decoding for 8-bit and high-precision monochrome processing."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from xp750_rip.performance import PerformanceTrace


class UnsupportedTiffError(ValueError):
    """Input requires TIFF semantics intentionally deferred beyond Phase 7."""


@dataclass(frozen=True, slots=True)
class Grayscale8Raster:
    width: int
    rows: Sequence[bytes]
    bits_per_sample: ClassVar[int] = 8

    def __post_init__(self) -> None:
        if self.width <= 0 or not self.rows:
            raise ValueError("grayscale raster dimensions must be positive")
        if getattr(self.rows, "width", None) == self.width:
            return
        if any(len(row) != self.width * (self.bits_per_sample // 8) for row in self.rows):
            raise ValueError("every grayscale row must match the declared width")

    @property
    def height(self) -> int:
        return len(self.rows)

    def decimate(self, factor: int) -> Grayscale8Raster:
        """Take every Nth source pixel in both axes with no interpolation."""

        if isinstance(factor, bool) or not isinstance(factor, int) or factor <= 0:
            raise ValueError("integer decimation factor must be positive")
        if self.bits_per_sample == 16:
            import numpy as np
            rows = tuple(np.frombuffer(row, dtype="<u2")[::factor].tobytes() for row in self.rows[::factor])
            return Grayscale16Raster(width=len(rows[0]) // 2, rows=rows)
        rows = tuple(bytes(row[::factor]) for row in self.rows[::factor])
        return Grayscale8Raster(width=len(rows[0]), rows=rows)


class Grayscale16Raster(Grayscale8Raster):
    """Little-endian uint16 grayscale; rows may be a bounded lazy producer."""

    bits_per_sample: ClassVar[int] = 16


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
    upstream_bits_per_sample: int | None = None
    row_source: object | None = None

    def close(self):
        if self.row_source is not None:
            self.row_source.close()


def _single_tag_int(value: object, name: str) -> int:
    if isinstance(value, tuple):
        if len(value) != 1:
            raise UnsupportedTiffError(f"{name} must contain exactly one value")
        value = value[0]
    if not isinstance(value, int):
        raise UnsupportedTiffError(f"{name} is missing or not an integer")
    return value


def decode_grayscale_tiff(
    path: Path, *, trace: PerformanceTrace | None = None, processing_bits: int = 8,
    page_index: int = 0, allow_color: bool = False,
) -> DecodedGrayscaleTiff:
    """Prepare a bounded row source; decoding happens only when rows are consumed."""
    import json

    from xp750_rip.input.tiff_rows import TiffRows, orient_raster

    rows = TiffRows(path, processing_bits, page_index=page_index)
    try:
        page = rows.page
        orientation = int(page.tags[274].value) if 274 in page.tags else 1
        compression = int(page.compression)
        if processing_bits == 8 and not allow_color:
            if rows.bits != 8 or rows.samples != 1 or rows.photo != 1:
                raise UnsupportedTiffError("only single-channel 8-bit BlackIsZero TIFF is accepted")
            if orientation != 1 or compression not in (1, 5):
                raise UnsupportedTiffError("8-bit input requires Orientation=1 and uncompressed or LZW TIFF")
        kind = Grayscale16Raster if processing_bits == 16 else Grayscale8Raster
        raster = orient_raster(kind(rows.width, rows), orientation)
        unit = int(page.tags[296].value) if 296 in page.tags else 1
        dpi = None
        if 282 in page.tags and 283 in page.tags and unit in (2, 3):
            def resolution(tag):
                value = page.tags[tag].value
                return value[0]/value[1] if isinstance(value, tuple) else float(value)
            multiplier = 2.54 if unit == 3 else 1.0
            dpi = (resolution(282)*multiplier, resolution(283)*multiplier)
            if orientation >= 5:
                dpi = dpi[::-1]
        upstream_bits = None
        try:
            metadata = json.loads(page.description or "{}")
            upstream = metadata.get("pytone_rip_upstream_bits")
            if isinstance(upstream, list) and upstream and all(type(v) is int and v in (1, 8, 16) for v in upstream):
                upstream_bits = max(upstream)
        except (ValueError, TypeError, AttributeError):
            pass
        if trace is not None:
            trace.event(f"TIFF row source (source={rows.bits}, processing={processing_bits})",
                        width=raster.width, height=raster.height, lazy=True,
                        source_segment_rows=rows.tile_height, cache_bytes=rows.cache_bytes)
        return DecodedGrayscaleTiff(path, raster, rows.bits, rows.photo, orientation,
                                    compression, dpi, 34675 in page.tags, upstream_bits, rows)
    except BaseException:
        rows.close()
        raise


def tiff_black_is_zero_to_qtr_code(sample: int) -> int:
    """Convert TIFF 0=black/255=white to QTR 0=white/255=black."""

    if isinstance(sample, bool) or not isinstance(sample, int) or not 0 <= sample <= 255:
        raise ValueError("TIFF grayscale sample must be an integer in 0..255")
    return 255 - sample
