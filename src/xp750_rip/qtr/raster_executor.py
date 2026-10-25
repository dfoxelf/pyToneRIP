# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Row-based exact-node execution of compiled QTR profiles."""

from array import array

import numpy as np

from xp750_rip.contone import LogicalChannelId, LogicalContonePlane, LogicalContoneRasterSet
from xp750_rip.input.tiff import Grayscale8Raster
from xp750_rip.qtr.quad import QuadCurve, QuadProfile

_POLARITY_TABLE = bytes(255 - value for value in range(256))


def black_is_zero_raster_to_qtr_codes(raster: Grayscale8Raster) -> tuple[bytes, ...]:
    """Apply the explicit Phase 7 polarity conversion to every decoded sample."""

    if raster.bits_per_sample == 16:
        from xp750_rip.streaming_raster import InvertedQtrRows
        return InvertedQtrRows(raster.rows, 16)
    return tuple(row.translate(_POLARITY_TABLE) for row in raster.rows)


def _execute_rows(code_rows: tuple[bytes, ...], curve: QuadCurve) -> tuple[array[int], ...]:
    lut = np.asarray(curve.samples, dtype=np.uint16)
    output: list[array[int]] = []
    for row in code_rows:
        values = lut[np.frombuffer(row, dtype=np.uint8)]
        packed = array("H")
        packed.frombytes(values.tobytes())
        output.append(packed)
    return tuple(output)


def execute_profile_codes(
    code_rows: tuple[bytes, ...], profile: QuadProfile
) -> LogicalContoneRasterSet:
    """Execute every compiled curve by exact 8-bit table indexing."""

    if not code_rows or not code_rows[0]:
        raise ValueError("QTR code raster dimensions must be positive")
    width = len(code_rows[0])
    if any(len(row) != width for row in code_rows):
        raise ValueError("all QTR code rows must share width")
    return LogicalContoneRasterSet(
        tuple(
            LogicalContonePlane(
                identity=LogicalChannelId(curve.ink_code),
                width_pixels=width,
                rows=_execute_rows(code_rows, curve),
            )
            for curve in profile.curves
        )
    )


def execute_curve_codes(
    code_rows: tuple[bytes, ...], curve: QuadCurve
) -> LogicalContonePlane:
    """Execute one identified table for bounded-memory N-channel dispatch."""

    if not code_rows or not code_rows[0]:
        raise ValueError("QTR code raster dimensions должны быть положительными")
    width = len(code_rows[0])
    if any(len(row) != width for row in code_rows):
        raise ValueError("Все строки QTR code raster должны иметь одинаковую ширину")
    return LogicalContonePlane(
        identity=LogicalChannelId(curve.ink_code),
        width_pixels=width,
        rows=_execute_rows(code_rows, curve),
    )
