# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Variable-coefficient error diffusion for the production QTR path.

The two base tables below are transcribed from the original publications:

* V. Ostromoukhov, *A Simple and Efficient Error-Diffusion Algorithm*,
  SIGGRAPH 2001, Appendix I.
* B. Zhou and X. Fang, *Improving mid-tone quality of variable-coefficient
  error diffusion using threshold modulation*, SIGGRAPH 2003, Tables 1-2.

The papers describe bilevel output.  pyToneRIP extends their quantizer to the
printer's existing variable-drop endpoints: each sample is quantized to one
of the two adjacent physical drop levels, and the resulting error is diffused
in the original continuous-tone scale. Endpoints (0, 65535) use the published
bilevel quantizer; Epson variable drops use its multilevel extension.
Separately, the sparse-page policy settles three zero-ink rows after content,
then clears diffusion state at the fourth. It is shared with streaming and
does not change coefficients or screen coordinates in nonblank regions.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from itertools import pairwise

import numpy as np

from xp750_rip.contone import UINT16_MAX, LogicalChannelId
from xp750_rip.dither.blank import EMPTY_SETTLE_ROWS, blank_row_spans
from xp750_rip.dither.ordered import GutenprintDropModel
from xp750_rip.dither.strategy import HalftoneAlgorithm
from xp750_rip.halftone import LogicalDotPlane

# Appendix I gives A10, A-11 and A01 for levels 0..127.  The normalized
# diffusion coefficients are A/M, where M is the sum of the three integers;
# D(i) == D(255-i) supplies the other half.  Keeping the source integers here
# (instead of rounded decimal constants) makes the provenance auditable.
OSTROMOUKHOV_2001_COEFFICIENTS_0_127: tuple[tuple[int, int, int], ...] = (
    (13, 0, 5), (13, 0, 5), (21, 0, 10), (7, 0, 4),
    (8, 0, 5), (47, 3, 28), (23, 3, 13), (15, 3, 8),
    (22, 6, 11), (43, 15, 20), (7, 3, 3), (501, 224, 211),
    (249, 116, 103), (165, 80, 67), (123, 62, 49), (489, 256, 191),
    (81, 44, 31), (483, 272, 181), (60, 35, 22), (53, 32, 19),
    (237, 148, 83), (471, 304, 161), (3, 2, 1), (481, 314, 185),
    (354, 226, 155), (1389, 866, 685), (227, 138, 125), (267, 158, 163),
    (327, 188, 220), (61, 34, 45), (627, 338, 505), (1227, 638, 1075),
    (20, 10, 19), (1937, 1000, 1767), (977, 520, 855), (657, 360, 551),
    (71, 40, 57), (2005, 1160, 1539), (337, 200, 247), (2039, 1240, 1425),
    (257, 160, 171), (691, 440, 437), (1045, 680, 627), (301, 200, 171),
    (177, 120, 95), (2141, 1480, 1083), (1079, 760, 513), (725, 520, 323),
    (137, 100, 57), (2209, 1640, 855), (53, 40, 19), (2243, 1720, 741),
    (565, 440, 171), (759, 600, 209), (1147, 920, 285), (2311, 1880, 513),
    (97, 80, 19), (335, 280, 57), (1181, 1000, 171), (793, 680, 95),
    (599, 520, 57), (2413, 2120, 171), (405, 360, 19), (2447, 2200, 57),
    (11, 10, 0), (158, 151, 3), (178, 179, 7), (1030, 1091, 63),
    (248, 277, 21), (318, 375, 35), (458, 571, 63), (878, 1159, 147),
    (5, 7, 1), (172, 181, 37), (97, 76, 22), (72, 41, 17),
    (119, 47, 29), (4, 1, 1), (4, 1, 1), (4, 1, 1),
    (4, 1, 1), (4, 1, 1), (4, 1, 1), (4, 1, 1),
    (4, 1, 1), (4, 1, 1), (65, 18, 17), (95, 29, 26),
    (185, 62, 53), (30, 11, 9), (35, 14, 11), (85, 37, 28),
    (55, 26, 19), (80, 41, 29), (155, 86, 59), (5, 3, 2),
    (5, 3, 2), (5, 3, 2), (5, 3, 2), (5, 3, 2),
    (5, 3, 2), (5, 3, 2), (5, 3, 2), (5, 3, 2),
    (5, 3, 2), (5, 3, 2), (5, 3, 2), (5, 3, 2),
    (305, 176, 119), (155, 86, 59), (105, 56, 39), (80, 41, 29),
    (65, 32, 23), (55, 26, 19), (335, 152, 113), (85, 37, 28),
    (115, 48, 37), (35, 14, 11), (355, 136, 109), (30, 11, 9),
    (365, 128, 107), (185, 62, 53), (25, 8, 7), (95, 29, 26),
    (385, 112, 103), (65, 18, 17), (395, 104, 101), (4, 1, 1),
)

# Zhou-Fang Table 1.  The article prints 214114 at key level 2; a frequently
# copied third-party table has 213113 there.  These are raw A values and are
# normalized at each key before the paper's linear interpolation of D.
ZHOU_FANG_2003_COEFFICIENT_KEYS: tuple[tuple[int, int, int, int], ...] = (
    (0, 13, 0, 5),
    (1, 1300249, 0, 499250),
    (2, 214114, 287, 99357),
    (3, 351854, 0, 199965),
    (4, 801100, 0, 490999),
    (10, 704075, 297466, 303694),
    (22, 46613, 31917, 21469),
    (32, 47482, 30617, 21900),
    (44, 43024, 42131, 14826),
    (64, 36411, 43219, 20369),
    (72, 38477, 53843, 7678),
    (77, 40503, 51547, 7948),
    (85, 35865, 34108, 30026),
    (95, 34117, 36899, 28983),
    (102, 35464, 35049, 29485),
    (107, 16477, 18810, 14712),
    (112, 33360, 37954, 28685),
    (127, 35269, 36066, 28664),
)

# Zhou-Fang Table 2: input-dependent threshold-modulation strength m(i).
ZHOU_FANG_2003_MODULATION_KEYS: tuple[tuple[int, float], ...] = (
    (0, 0.00),
    (44, 0.34),
    (64, 0.50),
    (85, 1.00),
    (95, 0.17),
    (102, 0.50),
    (107, 0.70),
    (112, 0.79),
    (127, 1.00),
)

_ALGORITHM_CODES = {
    HalftoneAlgorithm.OSTROMOUKHOV: 0,
    HalftoneAlgorithm.ZHOU_FANG: 1,
    HalftoneAlgorithm.ENTROPY_CONSTRAINED_ZHOU_FANG: 2,
}


def _mirror_half(values: np.ndarray) -> np.ndarray:
    """Mirror levels 0..127 around 127.5 as required by both papers."""

    return np.concatenate((values, values[::-1]), axis=0)


@lru_cache(maxsize=1)
def ostromoukhov_coefficients() -> np.ndarray:
    raw = np.asarray(OSTROMOUKHOV_2001_COEFFICIENTS_0_127, dtype=np.float64)
    half = raw / raw.sum(axis=1, keepdims=True)
    return np.ascontiguousarray(_mirror_half(half))


def _interpolate_key_rows(keys: tuple[tuple[int, ...], ...]) -> np.ndarray:
    result = np.empty((128, len(keys[0]) - 1), dtype=np.float64)
    for left, right in pairwise(keys):
        left_level, right_level = left[0], right[0]
        left_value = np.asarray(left[1:], dtype=np.float64)
        right_value = np.asarray(right[1:], dtype=np.float64)
        for level in range(left_level, right_level + 1):
            fraction = (level - left_level) / (right_level - left_level)
            result[level] = left_value + fraction * (right_value - left_value)
    return result


@lru_cache(maxsize=1)
def zhou_fang_coefficients() -> np.ndarray:
    # D, rather than the arbitrary-scale A integers, is the diffusion vector.
    # Normalize the published key rows first, then linearly interpolate D.
    normalized_keys = tuple(
        (level, *(value / (a10 + am11 + a01) for value in (a10, am11, a01)))
        for level, a10, am11, a01 in ZHOU_FANG_2003_COEFFICIENT_KEYS
    )
    return np.ascontiguousarray(_mirror_half(_interpolate_key_rows(normalized_keys)))


@lru_cache(maxsize=1)
def zhou_fang_modulation() -> np.ndarray:
    keys = tuple((level, value) for level, value in ZHOU_FANG_2003_MODULATION_KEYS)
    return np.ascontiguousarray(_mirror_half(_interpolate_key_rows(keys)[:, 0]))


@lru_cache(maxsize=1)
def binary_entropy_table() -> np.ndarray:
    """Dense H(p) table used with linear interpolation in the Cython loop.

    Calling two logarithms for every output pixel dominates the structure-aware
    mode at photo resolutions.  65536 intervals retain substantially more
    precision than the 8-bit source and keep the table small enough for CPU
    cache (about 512 KiB).  Linear interpolation avoids visible quantization of
    the threshold-modulation strength.
    """

    values = np.linspace(0.0, 1.0, 65537, dtype=np.float64)
    result = np.zeros_like(values)
    interior = values[1:-1]
    result[1:-1] = -interior * np.log(interior) - (1.0 - interior) * np.log(
        1.0 - interior
    )
    return np.ascontiguousarray(result)


def _gaussian_highpass(codes: np.ndarray, lut: np.ndarray) -> np.ndarray:
    """Return G_sigma=1(I) - I for entropy-constrained threshold modulation."""

    # A radius of four retains the usual sigma=1 Gaussian support.  Nearest
    # edge extension matches the formulation used to verify the
    # entropy-constrained reference.  Production uses the parallel Cython
    # separable convolution, avoiding SciPy in the compact packaged runtime.
    coordinates = np.arange(5, dtype=np.float64)
    weights = np.exp(-(coordinates * coordinates) / 2.0)
    weights /= weights[0] + 2.0 * weights[1:].sum()
    weights = np.ascontiguousarray(weights)
    from xp750_rip._cython_kernels import gaussian_highpass_qtr_kernel

    return gaussian_highpass_qtr_kernel(codes, lut, weights)


def _gaussian_highpass_band(
    code_rows: tuple[bytes, ...],
    lut: np.ndarray,
    start: int,
    end: int,
) -> np.ndarray:
    """Compute an exact high-pass band using four clamped halo rows per side."""

    height = len(code_rows)
    width = len(code_rows[0])
    source_indices = tuple(
        min(height - 1, max(0, row_index)) for row_index in range(start - 4, end + 4)
    )
    padded = np.frombuffer(
        b"".join(code_rows[row_index] for row_index in source_indices), dtype=np.uint8
    ).reshape(len(source_indices), width)
    # Every returned row is four samples away from the temporary boundary, so
    # the 9-tap separable convolution is identical to a monolithic full image.
    return np.ascontiguousarray(_gaussian_highpass(padded, lut)[4 : 4 + end - start])


@dataclass(frozen=True, slots=True)
class VariableCoefficientErrorDiffusion:
    """Fused QTR, multilevel quantization, ED and bit-packing strategy."""

    drops: GutenprintDropModel
    algorithm: HalftoneAlgorithm
    x_offset: int = 0
    y_offset: int = 0
    serpentine: bool = True
    random_seed: int = 0x5EED1234
    entropy_strength: float = 7.6

    def __post_init__(self) -> None:
        if self.algorithm not in _ALGORITHM_CODES:
            raise ValueError(f"{self.algorithm.value} is not an error-diffusion mode")
        if self.entropy_strength < 0:
            raise ValueError("entropy_strength must be non-negative")

    def dither_qtr_codes_placed(
        self,
        code_rows: tuple[bytes, ...],
        qtr_lut: tuple[int, ...],
        *,
        identity: LogicalChannelId,
        canvas_width: int,
        canvas_height: int,
        x: int,
        y: int,
    ) -> LogicalDotPlane:
        if not code_rows or not code_rows[0]:
            raise ValueError("QTR code raster dimensions must be positive")
        width, height = len(code_rows[0]), len(code_rows)
        if any(len(row) != width for row in code_rows):
            raise ValueError("all QTR code rows must share width")
        if len(qtr_lut) != 256 or any(not 0 <= value <= UINT16_MAX for value in qtr_lut):
            raise ValueError("QTR lookup table must contain 256 uint16 values")
        if canvas_width <= 0 or canvas_width % 8 or canvas_height <= 0:
            raise ValueError("invalid dither canvas dimensions")
        if x < 0 or y < 0 or x + width > canvas_width or y + height > canvas_height:
            raise ValueError("QTR code raster exceeds dither canvas bounds")

        lut = np.asarray(qtr_lut, dtype=np.uint16)
        coefficients = (
            ostromoukhov_coefficients()
            if self.algorithm is HalftoneAlgorithm.OSTROMOUKHOV
            else zhou_fang_coefficients()
        )
        modulation = (
            np.zeros(256, dtype=np.float64)
            if self.algorithm is HalftoneAlgorithm.OSTROMOUKHOV
            else zhou_fang_modulation()
        )
        endpoint_values = np.asarray(
            [endpoint.value / UINT16_MAX for endpoint in self.drops.endpoints],
            dtype=np.float64,
        )
        endpoint_codes = np.asarray(
            [endpoint.bits for endpoint in self.drops.endpoints], dtype=np.uint16
        )
        algorithm_code = _ALGORITHM_CODES[self.algorithm]
        # Bind one concrete compiled function for the complete job. Bands
        # only bound memory usage; they never re-select the algorithm.
        if self.algorithm is HalftoneAlgorithm.OSTROMOUKHOV:
            from xp750_rip._cython_kernels import (
                ostromoukhov_qtr_band_kernel as selected_band_kernel,
            )
        elif self.algorithm is HalftoneAlgorithm.ZHOU_FANG:
            from xp750_rip._cython_kernels import (
                zhou_fang_qtr_band_kernel as selected_band_kernel,
            )
        else:
            from xp750_rip._cython_kernels import (
                entropy_zhou_fang_qtr_band_kernel as selected_band_kernel,
            )
        bit_depth = self.drops.bit_depth
        packed = np.zeros((bit_depth, canvas_height, canvas_width // 8), dtype=np.uint8)
        error_state = np.zeros(width + 2, dtype=np.float64)
        entropy = binary_entropy_table()
        # Large printer modes can exceed 750 million pixels. Bands avoid
        # a second full-raster byte copy and a multi-gigabyte high-pass
        # allocation while carrying the ED state across every boundary.
        from xp750_rip._cython_kernels import qtr_active_rows_kernel

        empty_run = EMPTY_SETTLE_ROWS
        band_rows = 256
        for band_start in range(0, height, band_rows):
            band_end = min(band_start + band_rows, height)
            source_end = min(band_end + 1, height)
            band_codes = np.frombuffer(
                b"".join(code_rows[band_start:source_end]), dtype=np.uint8
            ).reshape(source_end - band_start, width)
            active = qtr_active_rows_kernel(band_codes[: band_end - band_start], lut)
            spans, empty_run = blank_row_spans(active, empty_run, settle_rows=EMPTY_SETTLE_ROWS)
            for local_start, local_end, render in spans:
                if not render:
                    error_state.fill(0)
                    continue
                start = band_start + local_start
                end = band_start + local_end
                codes = band_codes[local_start : local_end + 1]
                highpass = (
                    _gaussian_highpass_band(code_rows, lut, start, end)
                    if algorithm_code == 2
                    else np.empty((0, 0), dtype=np.float64)
                )
                packed_band, error_state = selected_band_kernel(
                    codes,
                    lut,
                    coefficients,
                    modulation,
                    endpoint_values,
                    endpoint_codes,
                    error_state,
                    highpass,
                    entropy,
                    end - start,
                    start,
                    height,
                    canvas_width,
                    x,
                    bit_depth,
                    self.serpentine,
                    self.random_seed & 0xFFFFFFFF,
                    x + self.x_offset,
                    y + self.y_offset + start,
                    self.entropy_strength,
                )
                packed[:, y + start : y + end] = packed_band

        return LogicalDotPlane(
            identity=identity,
            width_pixels=canvas_width,
            bit_plane_rows=tuple(tuple(row.tobytes() for row in bit_plane) for bit_plane in packed),
        )
