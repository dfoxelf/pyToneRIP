# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Bounded-memory QTR halftone production for the ESC/P2 backend.

The retained full-page dither methods remain the readable reference.  This
module changes only ownership and lifetime of buffers: it invokes the same AOT
kernels with the same global coordinates and carries error-diffusion state
between bands, but yields each physical band directly to the weave.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

import numpy as np

from xp750_rip.contone import UINT16_MAX, LogicalChannelId
from xp750_rip.dither.blue_noise import BlueNoiseFMDither, _channel_screen_parameters
from xp750_rip.dither.error_diffusion import (
    VariableCoefficientErrorDiffusion,
    _gaussian_highpass_band,
    binary_entropy_table,
    ostromoukhov_coefficients,
    zhou_fang_coefficients,
    zhou_fang_modulation,
)
from xp750_rip.dither.fast import GutenprintVeryFastDither
from xp750_rip.dither.ordered import GutenprintOrderedDither, _drop_lookup
from xp750_rip.dither.strategy import HalftoneAlgorithm
from xp750_rip.raster import (
    PhysicalPathId,
    PhysicalRasterBand,
    RasterBandPlane,
    StreamingPhysicalRasterSet,
)


class InvertedQtrRows(Sequence[bytes]):
    """Lazy TIFF BlackIsZero -> QTR polarity view without a page copy."""

    _table = bytes(255 - value for value in range(256))

    def __init__(self, rows: Sequence[bytes]) -> None:
        if not rows or not rows[0]:
            raise ValueError("QTR source rows must have positive dimensions")
        width = len(rows[0])
        if any(len(row) != width for row in rows):
            raise ValueError("all QTR source rows must share width")
        self._rows = rows
        self.width = width

    def __len__(self) -> int:
        return len(self._rows)

    def __getitem__(self, index: int | slice) -> bytes | tuple[bytes, ...]:
        if isinstance(index, slice):
            return tuple(row.translate(self._table) for row in self._rows[index])
        return self._rows[index].translate(self._table)


def _code_array(
    rows: Sequence[bytes], start: int, end: int, width: int
) -> np.ndarray:
    return np.frombuffer(
        b"".join(rows[index] for index in range(start, end)), dtype=np.uint8
    ).reshape(end - start, width)


def streaming_qtr_rasters(
    *,
    source_rows: Sequence[bytes],
    logical_inks: tuple[LogicalChannelId, ...],
    routing: dict[LogicalChannelId, PhysicalPathId],
    qtr_luts: dict[str, tuple[int, ...]],
    dither: (
        GutenprintOrderedDither
        | GutenprintVeryFastDither
        | BlueNoiseFMDither
        | VariableCoefficientErrorDiffusion
    ),
    canvas_width: int,
    available_paths: tuple[PhysicalPathId, ...] | None = None,
    allow_multiple_logical_to_one_path: bool = False,
    band_rows: int = 256,
    cancel_check: Callable[[], None] | None = None,
    on_dither_start: Callable[[], None] | None = None,
) -> StreamingPhysicalRasterSet:
    """Create a restartable all-channel band producer.

    The source height is the output height.  Page placement is handled by the
    backend's native horizontal/vertical offsets, exactly like the former
    non-legacy full-page path.
    """

    codes = InvertedQtrRows(source_rows)
    if canvas_width <= 0 or canvas_width % 8 or canvas_width < codes.width:
        raise ValueError("streaming dither canvas width is invalid")
    if band_rows <= 0:
        raise ValueError("streaming band height must be positive")
    if not logical_inks:
        raise ValueError("streaming dither needs at least one logical ink")
    if len(set(logical_inks)) != len(logical_inks):
        raise ValueError("streaming logical ink identities must be unique")
    missing = tuple(logical for logical in logical_inks if logical not in routing)
    if missing:
        raise ValueError(
            "No explicit routing for logical channels: "
            + ", ".join(logical.value for logical in missing)
        )
    paths = tuple(routing[logical] for logical in logical_inks)
    if available_paths is not None:
        unknown = set(paths) - set(available_paths)
        if unknown:
            raise ValueError(
                "Routing references unavailable PhysicalPaths: "
                + ", ".join(sorted(path.value for path in unknown))
            )
    if len(set(paths)) != len(paths):
        if allow_multiple_logical_to_one_path:
            raise NotImplementedError(
                "mixing multiple streaming logical inks into one PhysicalPath "
                "is not implemented"
            )
        raise ValueError(
            "multiple streaming logical inks target one PhysicalPath; mixing is disabled"
        )
    for logical in logical_inks:
        lut = qtr_luts.get(logical.value)
        if lut is None or len(lut) != 256 or any(not 0 <= value <= UINT16_MAX for value in lut):
            raise ValueError(f"invalid QTR LUT for {logical.value}")

    bit_depth = dither.drops.bit_depth
    height = len(codes)
    width = codes.width

    def produce():
        from xp750_rip._cython_kernels import (
            blue_noise_qtr_band_kernel,
            entropy_zhou_fang_qtr_band_kernel,
            ordered_qtr_band_kernel,
            ostromoukhov_qtr_band_kernel,
            very_fast_qtr_band_kernel,
            zhou_fang_qtr_band_kernel,
        )

        luts = {
            logical: np.asarray(qtr_luts[logical.value], dtype=np.uint16)
            for logical in logical_inks
        }
        error_states: dict[LogicalChannelId, np.ndarray] = {}
        coefficients = modulation = endpoints = endpoint_codes = entropy = None
        selected_error_kernel = None
        if isinstance(dither, VariableCoefficientErrorDiffusion):
            coefficients = (
                ostromoukhov_coefficients()
                if dither.algorithm is HalftoneAlgorithm.OSTROMOUKHOV
                else zhou_fang_coefficients()
            )
            modulation = (
                np.zeros(256, dtype=np.float64)
                if dither.algorithm is HalftoneAlgorithm.OSTROMOUKHOV
                else zhou_fang_modulation()
            )
            endpoints = np.asarray(
                [endpoint.value / UINT16_MAX for endpoint in dither.drops.endpoints],
                dtype=np.float64,
            )
            endpoint_codes = np.asarray(
                [endpoint.bits for endpoint in dither.drops.endpoints], dtype=np.uint16
            )
            entropy = binary_entropy_table()
            selected_error_kernel = {
                HalftoneAlgorithm.OSTROMOUKHOV: ostromoukhov_qtr_band_kernel,
                HalftoneAlgorithm.ZHOU_FANG: zhou_fang_qtr_band_kernel,
                HalftoneAlgorithm.ENTROPY_CONSTRAINED_ZHOU_FANG: (
                    entropy_zhou_fang_qtr_band_kernel
                ),
            }[dither.algorithm]
            error_states = {
                logical: np.zeros(width + 2, dtype=np.float64)
                for logical in logical_inks
            }

        matrix = None
        lower_bits = upper_bits = range_points = None
        if isinstance(dither, (GutenprintOrderedDither, BlueNoiseFMDither)):
            matrix = np.asarray(dither.matrix.thresholds, dtype=np.uint16).reshape(
                dither.matrix.height, dither.matrix.width
            )
            lower_bits, upper_bits, range_points = _drop_lookup(dither.drops.endpoints)
        elif isinstance(dither, GutenprintVeryFastDither):
            matrix = np.asarray(dither.matrix.thresholds, dtype=np.uint16).reshape(
                dither.matrix.height, dither.matrix.width
            )

        parallel_error_diffusion = (
            isinstance(dither, VariableCoefficientErrorDiffusion)
            and len(logical_inks) > 1
        )
        executor = (
            ThreadPoolExecutor(
                max_workers=min(
                    len(logical_inks), max(1, (os.cpu_count() or 2) // 2)
                ),
                thread_name_prefix="pytone-stream-ed",
            )
            if parallel_error_diffusion
            else None
        )
        try:
            if on_dither_start is not None:
                on_dither_start()
            for start in range(0, height, band_rows):
                if cancel_check is not None:
                    cancel_check()
                end = min(start + band_rows, height)
                source_end = min(end + 1, height)
                band_codes = _code_array(codes, start, source_end, width)

                def render_path(
                    item: tuple[LogicalChannelId, PhysicalPathId],
                    band_start: int = start,
                    band_end: int = end,
                    codes_for_band: np.ndarray = band_codes,
                ) -> tuple[
                    LogicalChannelId,
                    PhysicalPathId,
                    np.ndarray,
                    np.ndarray | None,
                ]:
                    logical, path = item
                    lut = luts[logical]
                    next_error = None
                    if isinstance(dither, VariableCoefficientErrorDiffusion):
                        assert selected_error_kernel is not None
                        assert coefficients is not None and modulation is not None
                        assert endpoints is not None and endpoint_codes is not None
                        assert entropy is not None
                        highpass = (
                            _gaussian_highpass_band(
                                codes, lut, band_start, band_end
                            )
                            if dither.algorithm
                            is HalftoneAlgorithm.ENTROPY_CONSTRAINED_ZHOU_FANG
                            else np.empty((0, 0), dtype=np.float64)
                        )
                        packed, next_error = selected_error_kernel(
                            codes_for_band,
                            lut,
                            coefficients,
                            modulation,
                            endpoints,
                            endpoint_codes,
                            error_states[logical],
                            highpass,
                            entropy,
                            band_end - band_start,
                            band_start,
                            height,
                            canvas_width,
                            0,
                            bit_depth,
                            dither.serpentine,
                            dither.random_seed & 0xFFFFFFFF,
                            dither.x_offset,
                            dither.y_offset + band_start,
                            dither.entropy_strength,
                        )
                    elif isinstance(dither, BlueNoiseFMDither):
                        assert matrix is not None
                        assert lower_bits is not None and upper_bits is not None
                        assert range_points is not None
                        phase_x, phase_y, channel_seed = _channel_screen_parameters(
                            logical, dither.matrix
                        )
                        packed = blue_noise_qtr_band_kernel(
                            codes_for_band[: band_end - band_start],
                            lut,
                            matrix,
                            lower_bits,
                            upper_bits,
                            range_points,
                            canvas_width,
                            0,
                            band_start,
                            dither.x_offset,
                            dither.y_offset,
                            phase_x,
                            phase_y,
                            channel_seed,
                            bit_depth,
                        )
                    elif isinstance(dither, GutenprintVeryFastDither):
                        assert matrix is not None
                        packed = very_fast_qtr_band_kernel(
                            codes_for_band[: band_end - band_start],
                            lut,
                            matrix,
                            canvas_width,
                            0,
                            band_start,
                            dither.x_offset,
                            dither.y_offset,
                            bit_depth,
                            dither.drops.endpoints[-1].bits,
                        )
                    else:
                        assert isinstance(dither, GutenprintOrderedDither)
                        assert matrix is not None
                        assert lower_bits is not None and upper_bits is not None
                        assert range_points is not None
                        packed = ordered_qtr_band_kernel(
                            codes_for_band[: band_end - band_start],
                            lut,
                            matrix,
                            lower_bits,
                            upper_bits,
                            range_points,
                            canvas_width,
                            0,
                            band_start,
                            dither.x_offset,
                            dither.y_offset,
                            bit_depth,
                        )
                    return logical, path, packed, next_error

                items = tuple(zip(logical_inks, paths, strict=True))
                rendered = (
                    executor.map(render_path, items)
                    if executor is not None
                    else map(render_path, items)
                )
                planes: list[RasterBandPlane] = []
                for logical, path, packed, next_error in rendered:
                    if next_error is not None:
                        error_states[logical] = next_error
                    planes.append(
                        RasterBandPlane(
                            path, np.ascontiguousarray(packed, dtype=np.uint8)
                        )
                    )
                yield PhysicalRasterBand(start, tuple(planes))
        finally:
            if executor is not None:
                executor.shutdown(wait=True)

    return StreamingPhysicalRasterSet(
        width_pixels=canvas_width,
        height_rows=height,
        bit_depth=bit_depth,
        identities=paths,
        band_factory=produce,
    )
