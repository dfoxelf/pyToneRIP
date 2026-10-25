# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""High-precision monochrome preparation and bounded Cython reconstruction."""

from array import array
from collections import OrderedDict
from collections.abc import Sequence
from threading import RLock

import numpy as np
from PIL import ImageOps

from xp750_rip.input.tiff import Grayscale16Raster


def _orient_samples(pixels, orientation):
    if orientation == 2:
        return pixels[:, ::-1]
    if orientation == 3:
        return pixels[::-1, ::-1]
    if orientation == 4:
        return pixels[::-1]
    if orientation == 5:
        return pixels.swapaxes(0, 1)
    if orientation == 6:
        return np.rot90(pixels, 3)
    if orientation == 7:
        return pixels.swapaxes(0, 1)[::-1, ::-1]
    if orientation == 8:
        return np.rot90(pixels)
    if orientation != 1:
        raise ValueError("TIFF orientation must be in range 1..8")
    return pixels


def _tiff_gray16(filename, frame_index=0):
    """Read unsigned TIFF samples unchanged, independently of the host OS."""
    import tifffile

    from xp750_rip._cython_kernels import gray16_from_rgb_kernel

    with tifffile.TiffFile(filename) as document:
        page = document.pages[frame_index]
        pixels = page.asarray(maxworkers=1)
        if pixels.dtype.kind != "u" or pixels.dtype.itemsize not in (1, 2):
            raise ValueError("Expected unsigned 8- or 16-bit TIFF samples")
        if page.planarconfig == 2:
            pixels = np.moveaxis(pixels, 0, -1)
        if pixels.ndim == 2:
            pixels = pixels[:, :, None]
        if pixels.ndim != 3:
            raise ValueError("Expected a two-dimensional TIFF page")
        photo = int(page.photometric)
        cmyk = photo == 5
        base_channels = 4 if cmyk else 3 if photo == 2 else 1
        if photo not in (0, 1, 2, 5):
            raise ValueError("16-bit TIFF requires grayscale, RGB or CMYK samples")
        if pixels.shape[2] < base_channels:
            raise ValueError("TIFF has fewer samples than its photometric format requires")
        alpha = int(page.extrasamples[0]) if page.extrasamples else 0
        if alpha not in (0, 1, 2):
            raise ValueError("Unsupported TIFF extra-sample format")
        pixels = pixels[:, :, : base_channels + int(alpha != 0)]
        if pixels.shape[2] != base_channels + int(alpha != 0):
            raise ValueError("TIFF alpha sample is missing")
        if photo == 0:
            pixels = pixels.copy()
            pixels[:, :, 0] = np.iinfo(pixels.dtype).max - pixels[:, :, 0]
        orientation = int(page.tags[274].value) if 274 in page.tags else 1
        pixels = _orient_samples(pixels, orientation)
        dtype = np.uint16 if pixels.dtype.itemsize == 2 else np.uint8
        return gray16_from_rgb_kernel(np.ascontiguousarray(pixels, dtype=dtype), alpha == 1, cmyk)


def image_to_gray16(image):
    from xp750_rip._cython_kernels import gray16_from_rgb_kernel

    bits = image.tag_v2.get(258, 8) if hasattr(image, "tag_v2") else 8
    source_bits = max(bits) if isinstance(bits, tuple) else bits
    if source_bits not in (1, 8, 16):
        raise ValueError("Expected unsigned 1-, 8- or 16-bit image samples")
    if source_bits == 16 and hasattr(image, "tag_v2"):
        return _tiff_gray16(image.filename, image.tell())
    oriented = ImageOps.exif_transpose(image)
    if oriented.mode.startswith("I;16"):
        return np.ascontiguousarray(np.asarray(oriented), dtype=np.uint16)
    if oriented.mode in ("I", "F"):
        raise ValueError("Expected unsigned 8- or 16-bit image samples")
    if oriented.mode not in ("L", "LA", "RGB", "RGBA"):
        oriented = oriented.convert("RGBA" if "transparency" in oriented.info else "RGB")
    pixels = np.asarray(oriented)
    if pixels.ndim == 2:
        pixels = pixels[:, :, None]
    return gray16_from_rgb_kernel(np.ascontiguousarray(pixels))


def promote_gray16(raster):
    if raster.bits_per_sample == 16:
        return raster
    rows = tuple(
        (np.frombuffer(row, dtype=np.uint8).astype(np.uint16) * 257).tobytes()
        for row in raster.rows
    )
    return Grayscale16Raster(raster.width, rows)


def compile_mono16_luts(runtime, inks, *, adjustments=None, density=1.0):
    from xp750_rip._cython_kernels import compile_mono16_luts_kernel

    if runtime is None:
        curves = np.zeros((1, len(inks), 256), dtype=np.uint16)
        weights = np.full((3, 1), 100.0)
        split = False
    else:
        curves = np.array(
            [[p.curve(ink).samples for ink in inks] for p in runtime.profiles], dtype=np.uint16
        )
        weights = np.array(
            (
                runtime.blend.weights.highlights,
                runtime.blend.weights.midtones,
                runtime.blend.weights.shadows,
            ),
            dtype=np.float64,
        )
        split = runtime.blend.split_tone
        adjustments = runtime.adjustments
    values = compile_mono16_luts_kernel(
        curves, weights, adjustments.gamma, adjustments.ink_limit, density, split, runtime is None
    )
    return dict(zip(inks, values, strict=True))


def code16_row(raw):
    from xp750_rip._cython_kernels import invert_gray16_kernel

    row = array("H")
    row.frombytes(invert_gray16_kernel(np.frombuffer(raw, dtype="<u2")).tobytes())
    return row


def _axis(source, target, method, start=0, end=None):
    end = target if end is None else end
    if source == target:
        return np.arange(start, end, dtype=np.int64)[:, None], np.ones((end-start, 1), dtype=np.float64)
    scale = source / target
    centers = (np.arange(start, end, dtype=np.float64) + 0.5) * scale - 0.5
    if method == "nearest":
        return (
            np.clip(np.floor(centers + 0.5), 0, source - 1).astype(np.int64)[:, None],
            np.ones((end-start, 1), dtype=np.float64),
        )
    support = {"bilinear": 1, "bicubic": 2, "mitchell": 2, "lanczos": 3}[method]
    widen = max(1.0, scale)
    radius = support * widen
    taps = int(np.ceil(2 * radius)) + 1
    indices = np.ceil(centers[:, None] - radius).astype(np.int64) + np.arange(taps, dtype=np.int64)
    distance = np.abs((indices - centers[:, None]) / widen)
    if method == "bilinear":
        weights = np.maximum(1.0 - distance, 0)
    elif method == "bicubic":
        weights = np.where(
            distance < 1,
            (1.5 * distance - 2.5) * distance**2 + 1,
            np.where(distance < 2, ((-0.5 * distance + 2.5) * distance - 4) * distance + 2, 0),
        )
    elif method == "mitchell":
        from xp750_rip.resampling import _mitchell_kernel

        weights = _mitchell_kernel(distance)
    else:
        weights = np.where(distance < 3, np.sinc(distance) * np.sinc(distance / 3), 0)
    weights[(indices < 0) | (indices >= source)] = 0
    weights /= weights.sum(axis=1, keepdims=True)
    return np.ascontiguousarray(np.clip(indices, 0, source - 1)), np.ascontiguousarray(weights)


class ResampledRows(Sequence):
    """Restartable, thread-safe reconstruction with bounded horizontal cache."""

    def __init__(self, raster, width, height, method):
        if width <= 0 or height <= 0:
            raise ValueError("resampled dimensions must be positive")
        self.source = raster
        self.width, self.height = width, height
        self.x_indices, self.x_weights = _axis(raster.width, width, method)
        self.method = method
        self.bits = raster.bits_per_sample
        self.dtype = np.uint16 if self.bits == 16 else np.uint8
        self.cache = OrderedDict()
        self.cache_rows = max(1, (32 * 1024 * 1024) // (width * 8))
        self.band_start = -1
        self.band = None
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
        from xp750_rip._cython_kernels import (
            resample_accumulate_row_kernel,
            resample_horizontal16_kernel,
            resample_vertical16_kernel,
        )

        with self.lock:
            # Limit the gather workspace as well as the persistent row cache.
            # A single downsampling footprint is the irreducible minimum.
            support = {"nearest": 0, "bilinear": 1, "bicubic": 2, "mitchell": 2, "lanczos": 3}[self.method]
            footprint = max(1, int(np.ceil(2*support*max(1.0,self.source.height/self.height)))+1)
            band_rows = max(1, min(32, self.cache_rows // footprint))
            start = index // band_rows * band_rows
            if start != self.band_start:
                end = min(start + band_rows, self.height)
                y_indices, y_weights = _axis(self.source.height, self.height, self.method, start, end)
                needed = np.unique(y_indices)
                if len(needed) > self.cache_rows:
                    # Extreme downsampling must not gather an entire source
                    # footprint. Accumulate one horizontal row at a time.
                    self.cache.clear()
                    values = np.zeros(self.width, dtype=np.float64)
                    for y, weight in zip(y_indices[0], y_weights[0], strict=True):
                        if weight == 0:
                            continue
                        row = resample_horizontal16_kernel(
                            np.frombuffer(self.source.rows[int(y)], dtype=self.dtype),
                            self.x_indices, self.x_weights,
                        )
                        resample_accumulate_row_kernel(values, row, weight)
                    np.clip(values, 0, (1 << self.bits)-1, out=values)
                    self.band = np.floor(values+0.5).astype(self.dtype)[None, :]
                    self.band_start = start
                    return self.band[0].tobytes()
                horizontal = []
                for y in needed:
                    row = self.cache.get(int(y))
                    if row is None:
                        row = resample_horizontal16_kernel(
                            np.frombuffer(self.source.rows[int(y)], dtype=self.dtype),
                            self.x_indices,
                            self.x_weights,
                        )
                        self.cache[int(y)] = row
                        while len(self.cache) > self.cache_rows:
                            self.cache.popitem(last=False)
                    else:
                        self.cache.move_to_end(int(y))
                    horizontal.append(row)
                local = np.ascontiguousarray(
                    np.searchsorted(needed, y_indices), dtype=np.int64
                )
                self.band = resample_vertical16_kernel(
                    np.ascontiguousarray(horizontal), local, y_weights, (1 << self.bits)-1
                )
                if self.bits == 8:
                    self.band = self.band.astype(np.uint8)
                self.band_start = start
            return self.band[index - start].tobytes()


def resize_grayscale_rows(raster, width, height, method):
    if (raster.width, raster.height) == (width, height):
        return raster
    from xp750_rip.input.tiff import Grayscale8Raster
    raster_type = Grayscale16Raster if raster.bits_per_sample == 16 else Grayscale8Raster
    return raster_type(width, ResampledRows(raster, width, height, str(method)))


# Existing callers of the high-precision helper keep the same interface.
resize_gray16 = resize_grayscale_rows
