# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Deterministic void-and-cluster blue-noise/FM threshold screen.

The expensive mask optimization is an offline development operation.  The RIP
loads the checked-in rank mask once and sends all per-pixel screening through
the same AOT-Cython multi-drop kernel as Ordered.  This preserves deterministic
reprints and makes runtime cost independent of how the mask was designed.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from hashlib import sha256
from importlib.resources import files

import numpy as np

from xp750_rip.contone import UINT16_MAX, LogicalChannelId
from xp750_rip.dither.ordered import (
    GutenprintDropModel,
    OrderedDitherMatrix,
    _drop_lookup,
)
from xp750_rip.halftone import LogicalDotPlane

_CANONICAL_MONO_INKS = ("K", "C", "M", "Y", "LC", "LM")
_CHANNEL_GRID_EDGE = 4  # 1 + ceil(sqrt(6)), exactly as Gutenprint uses for six inks.
_SCREEN_SEED = 0xB10E750

_MASKS = {
    "matrix-1x1": (
        "blue-noise-vac-128.u16le",
        128,
        128,
        "b0f507a80f97147ede3d9b975800af1502843461069dcd9eb9bc1155de5df77f",
    ),
    "matrix-2x1": (
        "blue-noise-vac-2x1-182x91.u16le",
        182,
        91,
        "0efa18e5b55dbe6a36dec127ac7ffcb5275647fb7384e36971a81add8dd64413",
    ),
    "matrix-4x1": (
        "blue-noise-vac-4x1-256x64.u16le",
        256,
        64,
        "0637637b7006751caacc0602ca5316300438c41563c15dc64575f22d2944450a",
    ),
}


@lru_cache(maxsize=4)
def load_blue_noise_fm_matrix(matrix_id: str = "matrix-1x1") -> OrderedDitherMatrix:
    """Load the progressive VAC mask matching the mode's physical DPI aspect."""

    if matrix_id == "matrix-1x2":
        return load_blue_noise_fm_matrix("matrix-2x1").transposed()
    try:
        filename, width, height, expected_hash = _MASKS[matrix_id]
    except KeyError as error:
        raise ValueError(f"unsupported Blue Noise FM aspect matrix: {matrix_id}") from error
    count = width * height
    resource = files("xp750_rip.dither").joinpath("data", filename)
    raw = resource.read_bytes()
    if len(raw) != count * 2:
        raise RuntimeError("blue-noise mask has an unexpected byte count")
    digest = sha256(raw).hexdigest()
    if digest != expected_hash:
        raise RuntimeError("blue-noise mask failed its source hash check")
    ranks = np.frombuffer(raw, dtype="<u2")
    if not np.array_equal(np.sort(ranks), np.arange(count, dtype=np.uint16)):
        raise RuntimeError("blue-noise mask is not a complete rank permutation")
    thresholds = tuple((ranks.astype(np.uint32) * 65536 // count).astype(np.uint16))
    return OrderedDitherMatrix(
        width=width,
        height=height,
        thresholds=thresholds,
        source=(
            "pyToneRIP void-and-cluster progressive blue-noise mask v1 "
            f"({matrix_id.removeprefix('matrix-')}, {width}x{height})"
        ),
    )


def _channel_screen_parameters(
    identity: LogicalChannelId,
    matrix: OrderedDitherMatrix,
) -> tuple[int, int, int]:
    """Return Gutenprint-style phase separation plus a stable tiling seed."""

    try:
        index = _CANONICAL_MONO_INKS.index(identity.value)
    except ValueError:
        # Imported printers may define other logical ink names.  Deriving an
        # index from SHA-256 is deterministic across Python processes, unlike
        # the randomized built-in hash().
        index = int.from_bytes(
            sha256(identity.value.encode("utf-8")).digest()[:2], "little"
        )
    phase_x = (matrix.width // _CHANNEL_GRID_EDGE) * (index % _CHANNEL_GRID_EDGE)
    phase_y = (matrix.height // _CHANNEL_GRID_EDGE) * (
        (index // _CHANNEL_GRID_EDGE) % _CHANNEL_GRID_EDGE
    )
    seed = (_SCREEN_SEED ^ ((index + 1) * 0x9E3779B1)) & 0xFFFFFFFF
    return phase_x, phase_y, seed


@dataclass(frozen=True, slots=True)
class BlueNoiseFMDither:
    """Progressive VAC screening without a visible short wallpaper period.

    The base threshold array remains a verified toroidal rank permutation.
    The Cython kernel deterministically translates and reflects each occurrence
    of that tile and decorrelates logical inks.  Every tile therefore retains
    identical mean coverage while repeated step-wedge fields no longer contain
    an exact 128-pixel square motif.
    """

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
        width, height = len(code_rows[0]), len(code_rows)
        if getattr(code_rows, "width", None) != width and any(len(row) != width for row in code_rows):
            raise ValueError("all QTR code rows must share width")
        if len(qtr_lut) not in (256, 65536) or any(not 0 <= value <= UINT16_MAX for value in qtr_lut):
            raise ValueError("QTR lookup table must contain 256 or 65536 uint16 values")
        if canvas_width <= 0 or canvas_width % 8 or canvas_height <= 0:
            raise ValueError("invalid dither canvas dimensions")
        if x < 0 or y < 0 or x + width > canvas_width or y + height > canvas_height:
            raise ValueError("QTR code raster exceeds dither canvas bounds")

        from xp750_rip._cython_kernels import blue_noise_qtr_band_kernel

        bit_depth = self.drops.bit_depth
        packed = np.zeros(
            (bit_depth, canvas_height, canvas_width // 8), dtype=np.uint8
        )
        matrix = np.asarray(self.matrix.thresholds, dtype=np.uint16).reshape(
            self.matrix.height, self.matrix.width
        )
        lut = np.asarray(qtr_lut, dtype=np.uint16)
        lower_bits, upper_bits, range_points = _drop_lookup(self.drops.endpoints)
        phase_x, phase_y, channel_seed = _channel_screen_parameters(
            identity, self.matrix
        )

        # Coordinate hashing happens only once per base tile; all pixel-level
        # lookup, variable-drop selection and packing remains inside Cython.
        band_rows = 128
        for local_start in range(0, height, band_rows):
            local_end = min(local_start + band_rows, height)
            codes = np.frombuffer(
                b"".join(code_rows[local_start:local_end]), dtype=np.uint16 if len(qtr_lut) == 65536 else np.uint8
            ).reshape(local_end - local_start, width)
            global_start = y + local_start
            global_end = y + local_end
            packed[:, global_start:global_end] = blue_noise_qtr_band_kernel(
                codes,
                lut,
                matrix,
                lower_bits,
                upper_bits,
                range_points,
                canvas_width,
                x,
                global_start,
                self.x_offset,
                self.y_offset,
                phase_x,
                phase_y,
                channel_seed,
                bit_depth,
            )
        return LogicalDotPlane(
            identity=identity,
            width_pixels=canvas_width,
            bit_plane_rows=tuple(
                tuple(row.tobytes() for row in bit_plane) for bit_plane in packed
            ),
        )
