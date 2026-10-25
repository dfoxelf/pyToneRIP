# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Separable Mitchell–Netravali reconstruction, B = C = 1/3.

The cubic is defined in Mitchell and Netravali, Reconstruction Filters in
Computer Graphics (1988), DOI: 10.1145/378456.378514. This is an independent
implementation of the published mathematics, not a binary-compatible renderer.
Pixel centers map to pixel centers. Reduction widens the filter independently
on each axis to suppress aliasing. Truncated edge weights are renormalized.
Both passes retain float64 values, including negative lobes, until final u8
quantization. No transfer-function conversion is applied to calibrated codes.
"""

from collections import OrderedDict
from math import ceil

import numpy as np
from PIL import Image


def _mitchell_kernel(distance: np.ndarray) -> np.ndarray:
    x = np.abs(distance)
    # B = C = 1/3, with the polynomial expressed in Horner form.
    inner = ((7.0 * x - 12.0) * x * x + 16.0 / 3.0) / 6.0
    outer = (((-7.0 / 3.0 * x + 12.0) * x - 20.0) * x + 32.0 / 3.0) / 6.0
    return np.where(x < 1.0, inner, np.where(x < 2.0, outer, 0.0))


def _axis_weights(source: int, target: int) -> tuple[np.ndarray, np.ndarray]:
    if source == target:
        return np.arange(source, dtype=np.intp)[:, None], np.ones((target, 1))
    scale = source / target
    support_scale = max(1.0, scale)
    radius = 2.0 * support_scale
    centers = (np.arange(target, dtype=np.float64) + 0.5) * scale - 0.5
    starts = np.ceil(centers - radius).astype(np.intp)
    taps = ceil(2.0 * radius) + 1
    indices = starts[:, None] + np.arange(taps, dtype=np.intp)
    weights = _mitchell_kernel((indices - centers[:, None]) / support_scale)
    weights[(indices < 0) | (indices >= source)] = 0.0
    weights /= weights.sum(axis=1, keepdims=True)
    # Out-of-image entries have zero weights; clamp only to make gathers safe.
    np.clip(indices, 0, source - 1, out=indices)
    return indices, weights


def resize_mitchell(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    """Resize an L image without an intermediate 8-bit horizontal raster.

    Floating-point row cache is bounded to 32 MiB (or one row if larger).
    Source and destination images remain complete 8-bit rasters; this function
    does not claim to make the upstream full-page layout a streaming decoder.
    """
    width, height = size
    if image.mode != "L":
        raise ValueError("Mitchell resampling requires an 8-bit grayscale image")
    if width <= 0 or height <= 0:
        raise ValueError("Resampled dimensions must be positive")
    if image.size == size:
        return image
    x_indices, x_weights = _axis_weights(image.width, width)
    y_indices, y_weights = _axis_weights(image.height, height)
    source = np.asarray(image)
    result = np.empty((height, width), dtype=np.uint8)
    cache: OrderedDict[int, np.ndarray] = OrderedDict()
    cache_rows = max(1, (32 * 1024 * 1024) // (width * 8))
    for y in range(height):
        row = np.zeros(width, dtype=np.float64)
        for index, weight in zip(y_indices[y], y_weights[y], strict=True):
            if weight == 0.0:
                continue
            source_y = int(index)
            horizontal = cache.get(source_y)
            if horizontal is None:
                horizontal = np.einsum(
                    "ij,ij->i", source[source_y][x_indices], x_weights,
                    dtype=np.float64,
                )
                if len(cache) >= cache_rows:
                    cache.popitem(last=False)
                cache[source_y] = horizontal
            else:
                cache.move_to_end(source_y)
            row += weight * horizontal
        np.clip(row, 0.0, 255.0, out=row)
        result[y] = np.floor(row + 0.5).astype(np.uint8)
    return Image.fromarray(result)
