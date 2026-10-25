# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Demand-driven TIFF rows with bounded strip/tile caches and explicit lifetime."""

from collections import OrderedDict
from collections.abc import Sequence
from contextlib import suppress
from math import ceil
from threading import RLock

import numpy as np
import tifffile


class TiffRows(Sequence):
    def __init__(self, path, processing_bits, *, page_index=0, cache_bytes=32 * 1024 * 1024):
        self.path = path
        self.page_index = page_index
        self.processing_bits = processing_bits
        self.document = tifffile.TiffFile(path)
        self.mapping = None
        self.lock = RLock()
        self.cache = OrderedDict()
        self.cached_bytes = 0
        self.cache_bytes = cache_bytes
        self.closed = False
        self.rotated_start, self.rotated = -1, None
        try:
            self.page = page = self.document.pages[page_index]
            self.width, self.height = page.imagewidth, page.imagelength
            self.bits = int(page.bitspersample)
            if page.dtype.kind not in ("u", "b") or self.bits not in (1, 8, 16):
                raise ValueError("Expected unsigned 1-, 8- or 16-bit TIFF samples")
            if processing_bits not in (8, 16) or self.width <= 0 or self.height <= 0:
                raise ValueError("Invalid TIFF geometry or processing precision")
            self.photo = int(page.photometric)
            if self.photo not in (0, 1, 2, 3, 5):
                raise ValueError("Unsupported TIFF photometric format")
            self.samples = int(page.samplesperpixel)
            self.separate = page.planarconfig == 2
            self.tile_width = page.tilewidth if page.is_tiled else self.width
            self.tile_height = (
                page.tilelength if page.is_tiled else min(page.rowsperstrip, self.height)
            )
            self.columns = ceil(self.width / self.tile_width)
            self.segments_per_plane = self.columns * ceil(self.height / self.tile_height)
            sample_bytes = max(1, page.dtype.itemsize)
            row_bytes = self.width * self.samples * sample_bytes
            self.decode_limit = max(64 * 1024 * 1024, row_bytes)
            decoded_size = (
                self.tile_width
                * self.tile_height
                * sample_bytes
                * (1 if self.separate else self.samples)
            )
            if page.is_memmappable:
                self.mapping = tifffile.memmap(path, page=page_index, mode="r")
            elif decoded_size > self.decode_limit:
                raise ValueError(
                    f"TIFF segment needs {decoded_size} decoded bytes; bounded reader limit is "
                    f"{self.decode_limit}. Save TIFF with smaller strips or tiles."
                )
            self.peak_cache_bytes = 0
        except BaseException:
            self.close()
            raise

    def __len__(self):
        return self.height

    def __del__(self):
        with suppress(AttributeError, OSError, ValueError):
            self.close()

    def close(self):
        with self.lock:
            self.cache.clear()
            self.rotated = None
            self.rotated_start = -1
            self.cached_bytes = 0
            if self.mapping is not None:
                self.mapping._mmap.close()
                self.mapping = None
            self.document.close()
            self.closed = True

    def _segment(self, index):
        if index in self.cache:
            self.cache.move_to_end(index)
            return self.cache[index]
        page = self.page
        count = page.databytecounts[index]
        if count > 2 * self.decode_limit:
            raise ValueError("Compressed TIFF segment exceeds bounded input buffer")
        handle = self.document.filehandle
        handle.seek(page.dataoffsets[index])
        raw = handle.read(count)
        if len(raw) != count:
            raise ValueError("Truncated TIFF segment")
        decoded, _position, shape = page.decode(raw, index, jpegtables=page.jpegtables)
        if decoded is None:
            decoded = np.zeros(shape, dtype=page.dtype)
        decoded = decoded[0]
        budget = max(self.cache_bytes, decoded.nbytes)
        while self.cache and self.cached_bytes + decoded.nbytes > budget:
            _, old = self.cache.popitem(last=False)
            self.cached_bytes -= old.nbytes
        self.cache[index] = decoded
        self.cached_bytes += decoded.nbytes
        self.peak_cache_bytes = max(self.peak_cache_bytes, self.cached_bytes)
        return decoded

    def _pixels(self, y):
        if self.mapping is not None:
            values = self.mapping[:, y, :] if self.separate else self.mapping[y]
            if self.separate:
                values = np.moveaxis(values, 0, -1)
            return values[:, None] if values.ndim == 1 else values
        result = np.empty((self.width, self.samples), dtype=self.page.dtype)
        for plane in range(self.samples if self.separate else 1):
            for column in range(self.columns):
                index = (
                    plane * self.segments_per_plane
                    + (y // self.tile_height) * self.columns
                    + column
                )
                decoded = self._segment(index)
                start = column * self.tile_width
                count = min(self.tile_width, self.width - start)
                row = decoded[y % self.tile_height, :count]
                if self.separate:
                    result[start : start + count, plane] = row[:, 0]
                else:
                    result[start : start + count] = row
        return result

    def raw_oriented_row(self, index, orientation=1):
        """Preserve channel selectors before grayscale conversion."""
        with self.lock:
            self._ensure_open()
            if orientation <= 4:
                row = self._pixels(self.height - 1 - index if orientation in (3, 4) else index)
                return row[::-1] if orientation in (2, 3) else row
            position = self.width - 1 - index if orientation in (5, 6) else index
            rows = max(
                1,
                min(
                    32,
                    (16 * 1024 * 1024) // (self.height * self.samples * self.page.dtype.itemsize),
                ),
            )
            start = position // rows * rows
            if start != self.rotated_start:
                end = min(start + rows, self.width)
                self.rotated = np.empty(
                    (end - start, self.height, self.samples), dtype=self.page.dtype
                )
                for y in range(self.height):
                    self.rotated[:, y] = self._pixels(y)[self.width - end : self.width - start][
                        ::-1
                    ]
                self.rotated_start = start
            row = self.rotated[position - start]
            return row[::-1] if orientation in (6, 7) else row

    def _ensure_open(self):
        if self.closed:
            # Layout views can be consumed again after a completed render.
            self.document = tifffile.TiffFile(self.path)
            self.page = self.document.pages[self.page_index]
            if self.page.is_memmappable:
                self.mapping = tifffile.memmap(self.path, page=self.page_index, mode="r")
            self.closed = False

    def __getitem__(self, index):
        if isinstance(index, slice):
            return tuple(self[i] for i in range(*index.indices(self.height)))
        if index < 0:
            index += self.height
        if not 0 <= index < self.height:
            raise IndexError(index)
        with self.lock:
            self._ensure_open()
            pixels = self._pixels(index)
            dtype = np.uint16 if self.bits == 16 else np.uint8
            if self.photo == 3:
                pixels = self.page.colormap[:, pixels[:, 0]].T
                dtype = np.uint16
            if self.bits == 1:
                pixels = pixels.astype(np.uint8) * 255
            if self.photo == 0:
                pixels = pixels.copy()
                pixels[:, 0] = np.iinfo(dtype).max - pixels[:, 0]
            cmyk = self.photo == 5
            base = 4 if cmyk else 3 if self.photo in (2, 3) else 1
            alpha = int(self.page.extrasamples[0]) if self.page.extrasamples else 0
            pixels = np.ascontiguousarray(
                pixels[None, :, : base + int(alpha in (1, 2))], dtype=dtype
            )
            from xp750_rip._cython_kernels import gray8_from_rgb_kernel, gray16_from_rgb_kernel

            if self.processing_bits == 8 and dtype == np.uint8 and not cmyk:
                return gray8_from_rgb_kernel(pixels, alpha == 1)[0].tobytes()
            gray = gray16_from_rgb_kernel(pixels, alpha == 1, cmyk)[0]
            if self.processing_bits == 8:
                gray = ((gray.astype(np.uint32) + 128) // 257).astype(np.uint8)
            return gray.tobytes()


class CroppedRows(Sequence):
    def __init__(self, raster, left, top, right, bottom):
        self.source, self.left, self.top = raster, left, top
        self.width, self.height = right - left, bottom - top
        self.sample_bytes = raster.bits_per_sample // 8

    def __len__(self):
        return self.height

    def __getitem__(self, index):
        if isinstance(index, slice):
            return tuple(self[i] for i in range(*index.indices(self.height)))
        if index < 0:
            index += self.height
        if not 0 <= index < self.height:
            raise IndexError(index)
        row = self.source.rows[self.top + index]
        return row[self.left * self.sample_bytes : (self.left + self.width) * self.sample_bytes]


class RotatedRows(Sequence):
    """Quarter-turn view with a bounded output block, without a page allocation."""

    def __init__(self, raster):
        self.source = raster
        self.width, self.height = raster.height, raster.width
        self.dtype = np.uint16 if raster.bits_per_sample == 16 else np.uint8
        self.band_rows = max(
            1, min(32, (16 * 1024 * 1024) // (self.width * np.dtype(self.dtype).itemsize))
        )
        self.band_start, self.band = -1, None
        self.lock = RLock()

    def __len__(self):
        return self.height

    def __getitem__(self, index):
        if isinstance(index, slice):
            return tuple(self[i] for i in range(*index.indices(self.height)))
        if index < 0:
            index += self.height
        if not 0 <= index < self.height:
            raise IndexError(index)
        with self.lock:
            start = index // self.band_rows * self.band_rows
            if start != self.band_start:
                end = min(start + self.band_rows, self.height)
                self.band = np.empty((end - start, self.width), dtype=self.dtype)
                for y in range(self.source.height):
                    row = np.frombuffer(self.source.rows[y], dtype=self.dtype)
                    self.band[:, y] = row[self.source.width - end : self.source.width - start][::-1]
                self.band_start = start
            return self.band[index - start].tobytes()


class FlippedRows(Sequence):
    def __init__(self, raster, *, x=False, y=False):
        self.source, self.x, self.y = raster, x, y
        self.width, self.height = raster.width, raster.height
        self.dtype = np.uint16 if raster.bits_per_sample == 16 else np.uint8

    def __len__(self):
        return self.height

    def __getitem__(self, index):
        if isinstance(index, slice):
            return tuple(self[i] for i in range(*index.indices(self.height)))
        if index < 0:
            index += self.height
        if not 0 <= index < self.height:
            raise IndexError(index)
        row = self.source.rows[self.height - 1 - index if self.y else index]
        return np.frombuffer(row, dtype=self.dtype)[::-1].tobytes() if self.x else row


def orient_raster(raster, orientation):
    if orientation not in range(1, 9):
        raise ValueError("TIFF orientation must be in range 1..8")
    kind = type(raster)
    if orientation >= 5:
        raster = kind(raster.height, RotatedRows(raster))
    flip_x = orientation in (2, 3, 6, 7)
    flip_y = orientation in (3, 4, 5, 6)
    return (
        kind(raster.width, FlippedRows(raster, x=flip_x, y=flip_y)) if flip_x or flip_y else raster
    )
