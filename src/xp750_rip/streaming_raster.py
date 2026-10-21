# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Bounded-memory QTR halftone production for the ESC/P2 backend.

The retained full-page dither methods remain the readable reference.  This
module invokes AOT kernels with global coordinates and yields physical bands
directly to the weave. Empty ink rows bypass screening; error diffusion settles
three empty rows before resetting at a blank-region boundary, across bands.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from functools import partial

import numpy as np

from xp750_rip.contone import UINT16_MAX, LogicalChannelId
from xp750_rip.dither.blank import EMPTY_SETTLE_ROWS, blank_row_spans
from xp750_rip.dither.blue_noise import BlueNoiseFMDither, _channel_screen_parameters
from xp750_rip.dither.coupled import CoupledErrorDiffusion
from xp750_rip.dither.error_diffusion import (
    VariableCoefficientErrorDiffusion,
    _gaussian_highpass_band,
    binary_entropy_table,
    error_diffusion_tables,
    x2y1_channel_seed,
    x2y1_noise_sequence,
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


def _code_array(rows: Sequence[bytes], start: int, end: int, width: int) -> np.ndarray:
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
        | CoupledErrorDiffusion
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
                "mixing multiple streaming logical inks into one PhysicalPath is not implemented"
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

    if isinstance(dither, CoupledErrorDiffusion):
        def produce_coupled():
            if on_dither_start is not None:
                on_dither_start()
            sources = {ink: codes for ink in logical_inks}
            for start, packed in dither.iter_bands(
                sources, qtr_luts, logical_inks, canvas_width=canvas_width,
                band_rows=band_rows, cancel_check=cancel_check,
            ):
                yield PhysicalRasterBand(start, tuple(
                    RasterBandPlane(path, packed[ink])
                    for ink, path in zip(logical_inks, paths, strict=True)
                ))
        return StreamingPhysicalRasterSet(
            width_pixels=canvas_width, height_rows=height, bit_depth=bit_depth,
            identities=paths, band_factory=produce_coupled,
        )

    def produce():
        from xp750_rip._cython_kernels import (
            blue_noise_qtr_band_kernel,
            entropy_zhou_fang_qtr_band_kernel,
            ordered_qtr_band_kernel,
            ostromoukhov_qtr_band_kernel,
            qtr_active_rows_kernel,
            very_fast_qtr_band_kernel,
            x2y1_qtr_band_kernel,
            zhou_fang_qtr_band_kernel,
        )

        luts = {
            logical: np.asarray(qtr_luts[logical.value], dtype=np.uint16)
            for logical in logical_inks
        }
        empty_runs = {logical: EMPTY_SETTLE_ROWS for logical in logical_inks}
        error_states: dict[LogicalChannelId, np.ndarray] = {}
        coefficients = modulation = endpoints = endpoint_codes = entropy = None
        selected_error_kernel = None
        error_kernels = {}
        if isinstance(dither, VariableCoefficientErrorDiffusion):
            coefficients, modulation = error_diffusion_tables(dither.algorithm)
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
                HalftoneAlgorithm.X2Y1: x2y1_qtr_band_kernel,
                HalftoneAlgorithm.ENTROPY_CONSTRAINED_ZHOU_FANG: (
                    entropy_zhou_fang_qtr_band_kernel
                ),
            }[dither.algorithm]
            error_states = {
                logical: np.zeros(width + 2, dtype=np.float64) for logical in logical_inks
            }
            error_kernels = {logical: selected_error_kernel for logical in logical_inks}
            if dither.algorithm is HalftoneAlgorithm.X2Y1:
                error_kernels = {
                    logical: partial(
                        selected_error_kernel,
                        noise_samples=x2y1_noise_sequence(x2y1_channel_seed(dither.random_seed, logical)),
                        noise_index=np.zeros(1, dtype=np.intp),
                    ) for logical in logical_inks
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
            isinstance(dither, VariableCoefficientErrorDiffusion) and len(logical_inks) > 1
        )
        executor = (
            ThreadPoolExecutor(
                max_workers=min(len(logical_inks), max(1, (os.cpu_count() or 2) // 2)),
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
                    rows = band_end - band_start
                    current_error = error_states.get(logical)
                    active = qtr_active_rows_kernel(codes_for_band[:rows], lut)
                    spans, empty_runs[logical] = blank_row_spans(
                        active,
                        empty_runs[logical],
                        settle_rows=EMPTY_SETTLE_ROWS if current_error is not None else 0,
                    )
                    packed = np.zeros((bit_depth, rows, canvas_width // 8), dtype=np.uint8)
                    for local_start, local_end, render in spans:
                        if cancel_check is not None:
                            cancel_check()
                        if not render:
                            if current_error is not None:
                                current_error.fill(0)
                            continue
                        span_start = band_start + local_start
                        span_end = band_start + local_end
                        # Keep the next source row for exact clipped diffusion.
                        span_codes = codes_for_band[local_start : local_end + 1]
                        next_error = None
                        if isinstance(dither, VariableCoefficientErrorDiffusion):
                            assert selected_error_kernel is not None
                            assert coefficients is not None and modulation is not None
                            assert endpoints is not None and endpoint_codes is not None
                            assert entropy is not None
                            highpass = (
                                _gaussian_highpass_band(codes, lut, span_start, span_end)
                                if dither.algorithm
                                is HalftoneAlgorithm.ENTROPY_CONSTRAINED_ZHOU_FANG
                                else np.empty((0, 0), dtype=np.float64)
                            )
                            span_packed, next_error = error_kernels[logical](
                                span_codes,
                                lut,
                                coefficients,
                                modulation,
                                endpoints,
                                endpoint_codes,
                                current_error,
                                highpass,
                                entropy,
                                span_end - span_start,
                                span_start,
                                height,
                                canvas_width,
                                0,
                                bit_depth,
                                dither.serpentine,
                                dither.random_seed & 0xFFFFFFFF,
                                dither.x_offset,
                                dither.y_offset + span_start,
                                dither.entropy_strength,
                            )
                        elif isinstance(dither, BlueNoiseFMDither):
                            assert matrix is not None
                            assert lower_bits is not None and upper_bits is not None
                            assert range_points is not None
                            phase_x, phase_y, channel_seed = _channel_screen_parameters(
                                logical, dither.matrix
                            )
                            span_packed = blue_noise_qtr_band_kernel(
                                span_codes[: span_end - span_start],
                                lut,
                                matrix,
                                lower_bits,
                                upper_bits,
                                range_points,
                                canvas_width,
                                0,
                                span_start,
                                dither.x_offset,
                                dither.y_offset,
                                phase_x,
                                phase_y,
                                channel_seed,
                                bit_depth,
                            )
                        elif isinstance(dither, GutenprintVeryFastDither):
                            assert matrix is not None
                            span_packed = very_fast_qtr_band_kernel(
                                span_codes[: span_end - span_start],
                                lut,
                                matrix,
                                canvas_width,
                                0,
                                span_start,
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
                            span_packed = ordered_qtr_band_kernel(
                                span_codes[: span_end - span_start],
                                lut,
                                matrix,
                                lower_bits,
                                upper_bits,
                                range_points,
                                canvas_width,
                                0,
                                span_start,
                                dither.x_offset,
                                dither.y_offset,
                                bit_depth,
                            )
                        packed[:, local_start:local_end] = span_packed
                        if next_error is not None:
                            current_error = next_error
                    return logical, path, packed, current_error

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
                        RasterBandPlane(path, np.ascontiguousarray(packed, dtype=np.uint8))
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
