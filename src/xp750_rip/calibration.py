# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Raw calibration raster decoding.

Calibration targets bypass QUAD curves and colour management. The selected
printer mode's density, normal gamma and ink limit are applied later in the
shared raster pipeline. Targets are accepted only while calibration mode is
explicitly enabled in Preferences.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

RGB_SELECTOR_TO_INK = {
    (127, 255): "K",
    (191, 255): "C",
    (223, 255): "M",
    (239, 255): "Y",
    (247, 255): "LC",
    (251, 255): "LM",
    (253, 255): "LK",
    (254, 255): "LLK",
    (255, 127): "O",
    (255, 191): "G",
}


@dataclass(frozen=True, slots=True)
class CalibrationRaster:
    width: int
    rows_by_ink: dict[str, Sequence[bytes]]
    dpi: tuple[float, float] | None
    bits_per_sample: int = 8
    source_bits_per_sample: int = 8
    row_source: object | None = None

    def close(self):
        if self.row_source is not None:
            self.row_source.close()

    @property
    def height(self) -> int:
        return len(next(iter(self.rows_by_ink.values())))


class _CalibrationPlane:
    def __init__(self, provider, ink):
        self.provider, self.ink = provider, ink
        self.width = provider.width

    def __len__(self):
        return self.provider.height

    def __getitem__(self, index):
        if isinstance(index, slice):
            return tuple(self[i] for i in range(*index.indices(len(self))))
        if index < 0:
            index += len(self)
        if not 0 <= index < len(self):
            raise IndexError(index)
        return self.provider.row(self.ink, index)


class _CalibrationProvider:
    def __init__(self, path, inks, limit, bits, mode, page_index):
        from collections import OrderedDict
        from threading import RLock

        import numpy as np
        import tifffile

        from xp750_rip.input.tiff import decode_grayscale_tiff

        if not inks or not 0 <= limit <= 100:
            raise ValueError("Calibration requires selected inks and a limit in 0..100")
        if page_index is None:
            with tifffile.TiffFile(path) as document:
                if len(document.pages) != 1:
                    raise ValueError("Calibration mode requires a single-page TIFF")
        self.decoded = decode_grayscale_tiff(
            path, processing_bits=bits, page_index=page_index or 0, allow_color=True
        )
        self.inks, self.limit, self.bits, self.mode = inks, limit, bits, mode
        self.width, self.height = self.decoded.raster.width, self.decoded.raster.height
        self.dtype = np.uint16 if bits == 16 else np.uint8
        self.maximum = (1 << bits) - 1
        self.blank = bytes(self.width * (bits // 8))
        self.lock, self.cache = RLock(), OrderedDict()
        self.cache_rows = max(
            1, min(64, (8 * 1024 * 1024) // (len(inks) * self.width * (bits // 8)))
        )
        try:
            if mode == "per_channel":
                first, last = None, None
                for y in range(self.height):
                    gray = np.frombuffer(self.decoded.raster.rows[y], dtype=self.dtype)
                    if np.any(gray < 250 * (257 if bits == 16 else 1)):
                        first = y if first is None else first
                        last = y + 1
                if first is None:
                    raise ValueError("per-channel calibration TIFF contains no target rows")
                if last - first < len(inks):
                    raise ValueError("per-channel calibration TIFF is too short for selected inks")
                self.spans = [
                    (
                        first + (last - first) * i // len(inks),
                        first + (last - first) * (i + 1) // len(inks),
                    )
                    for i in range(len(inks))
                ]
                values = (self.maximum - np.arange(self.maximum + 1, dtype=np.uint32)) * limit
                self.coverage = ((values + (50 if bits == 16 else 0)) // 100).astype(self.dtype)
            else:
                if self.decoded.row_source.photo != 2:
                    raise ValueError("RGB selector calibration requires RGB samples")
                self.selectors = np.full((256, 256), -1, dtype=np.int16)
                for (r, b), ink in RGB_SELECTOR_TO_INK.items():
                    if ink == "K" and ink not in inks:
                        ink = next((name for name in ("PK", "MK") if name in inks), ink)
                    if ink in inks:
                        self.selectors[r, b] = inks.index(ink)
        except BaseException:
            self.close()
            raise

    def close(self):
        self.cache.clear()
        self.decoded.close()

    def row(self, ink, y):
        import numpy as np

        with self.lock:
            if self.mode == "per_channel":
                first, last = self.spans[ink]
                if not first <= y < last:
                    return self.blank
                gray = np.frombuffer(self.decoded.raster.rows[y], dtype=self.dtype)
                return self.coverage[gray].tobytes()
            if y not in self.cache:
                from xp750_rip._cython_kernels import calibration_selectors16_kernel

                source = self.decoded.row_source
                pixels = source.raw_oriented_row(y, self.decoded.orientation)
                dtype = np.uint16 if source.bits == 16 else np.uint8
                pixels = np.ascontiguousarray(pixels[None, :, :3], dtype=dtype)
                result = calibration_selectors16_kernel(
                    pixels, self.selectors, len(self.inks), self.limit if self.bits == 16 else 100
                )[:, 0]
                if self.bits == 8:
                    result = (result.astype(np.uint32) * self.limit // (100 * 257)).astype(np.uint8)
                self.cache[y] = tuple(row.tobytes() for row in result)
                while len(self.cache) > self.cache_rows:
                    self.cache.popitem(last=False)
            self.cache.move_to_end(y)
            return self.cache[y][ink]


def _decode(path, inks, ink_limit_percent, processing_bits, mode, page_index):
    provider = _CalibrationProvider(
        path, inks, ink_limit_percent, processing_bits, mode, page_index
    )
    return CalibrationRaster(
        provider.width,
        {ink: _CalibrationPlane(provider, i) for i, ink in enumerate(inks)},
        provider.decoded.dpi,
        processing_bits,
        provider.decoded.bits_per_sample,
        provider,
    )


def decode_rgb_selectors(path, inks, ink_limit_percent, *, processing_bits=8, page_index=None):
    return _decode(path, inks, ink_limit_percent, processing_bits, "rgb_selectors", page_index)


def decode_per_channel(path, inks, ink_limit_percent, *, processing_bits=8, page_index=None):
    return _decode(path, inks, ink_limit_percent, processing_bits, "per_channel", page_index)


class CalibrationGrayRows:
    def __init__(self, target, ink=None):
        self.target, self.ink = target, ink
        self.width = target.width

    def __len__(self):
        return self.target.height

    def __getitem__(self, index):
        import numpy as np

        if isinstance(index, slice):
            return tuple(self[i] for i in range(*index.indices(len(self))))
        if index < 0:
            index += len(self)
        if not 0 <= index < len(self):
            raise IndexError(index)
        dtype = np.uint16 if self.target.bits_per_sample == 16 else np.uint8
        if self.ink is None:
            values = np.zeros(self.width, dtype=dtype)
            for rows in self.target.rows_by_ink.values():
                np.maximum(values, np.frombuffer(rows[index], dtype=dtype), out=values)
        else:
            values = np.frombuffer(self.target.rows_by_ink[self.ink][index], dtype=dtype)
        return (((1 << self.target.bits_per_sample) - 1) - values).tobytes()
