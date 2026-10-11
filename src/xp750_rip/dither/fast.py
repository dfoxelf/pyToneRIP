# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Exact Gutenprint ``VeryFast`` variable-drop behavior.

``Fast`` itself uses the normal Ordered drop selection with Gutenprint's
small runtime-generated matrix, so it needs no separate pixel kernel.
``VeryFast`` deliberately sacrifices variable-drop gradation: any selected
pixel receives the largest available drop code.  The names Fast/VeryFast are
historical Gutenprint API names; in the fused, memory-bound pyToneRIP pipeline
they do not promise lower wall-clock time than Ordered.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from xp750_rip.contone import LogicalChannelId
from xp750_rip.dither.ordered import (
    GutenprintDropModel,
    OrderedDitherMatrix,
)
from xp750_rip.halftone import LogicalDotPlane


@dataclass(frozen=True, slots=True)
class GutenprintVeryFastDither:
    matrix: OrderedDitherMatrix
    drops: GutenprintDropModel
    x_offset: int = 0
    y_offset: int = 0

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
        width = len(code_rows[0])
        height = len(code_rows)
        if any(len(row) != width for row in code_rows):
            raise ValueError("all QTR code rows must share width")
        if len(qtr_lut) != 256 or any(not 0 <= value <= 65535 for value in qtr_lut):
            raise ValueError("QTR lookup table must contain 256 uint16 values")
        if canvas_width <= 0 or canvas_width % 8:
            raise ValueError("dither canvas width must be a positive multiple of 8")
        if canvas_height <= 0 or x < 0 or y < 0:
            raise ValueError("invalid dither canvas placement")
        if x + width > canvas_width or y + height > canvas_height:
            raise ValueError("QTR code raster exceeds dither canvas bounds")

        from xp750_rip._cython_kernels import very_fast_qtr_band_kernel

        bit_depth = self.drops.bit_depth
        row_bytes = canvas_width // 8
        packed = np.zeros((bit_depth, canvas_height, row_bytes), dtype=np.uint8)
        matrix = np.asarray(self.matrix.thresholds, dtype=np.uint16).reshape(
            self.matrix.height, self.matrix.width
        )
        lut = np.asarray(qtr_lut, dtype=np.uint16)
        largest_code = self.drops.endpoints[-1].bits
        band_rows = 128
        for local_start in range(0, height, band_rows):
            local_end = min(local_start + band_rows, height)
            codes = np.frombuffer(
                b"".join(code_rows[local_start:local_end]), dtype=np.uint8
            ).reshape(local_end - local_start, width)
            global_start = y + local_start
            global_end = y + local_end
            packed[:, global_start:global_end] = very_fast_qtr_band_kernel(
                codes,
                lut,
                matrix,
                canvas_width,
                x,
                global_start,
                self.x_offset,
                self.y_offset,
                bit_depth,
                largest_code,
            )
        return LogicalDotPlane(
            identity=identity,
            width_pixels=canvas_width,
            bit_plane_rows=tuple(
                tuple(row.tobytes() for row in bit_plane) for bit_plane in packed
            ),
        )
