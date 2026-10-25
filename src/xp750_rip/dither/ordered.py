# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Proven Gutenprint ``Ordered`` subset for variable-drop uint16 channels.

The implementation follows Gutenprint 5.3.5 ``dither-ordered.c`` and
``dither-inks.c``. Matrix and drop configuration are injected data, so this
module has no printer-model or PhysicalPath knowledge.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from hashlib import sha256
from importlib.resources import files
from itertools import pairwise
from xml.etree import ElementTree

import numpy as np

from xp750_rip.contone import UINT16_MAX, LogicalChannelId, LogicalContonePlane
from xp750_rip.halftone import LogicalDotPlane

_MATRIX_2X1_WIDTH = 367
_MATRIX_2X1_HEIGHT = 179
_MATRIX_2X1_COUNT = 65693
_MATRIX_2X1_U16LE_SHA256 = "1dc9749afefba8fc55075f4b6254035cfc718bdc275f47b4da2b165cb9316219"
_CYTHON_MIN_PIXELS = 1_000_000
_FAST_MATRIX_EDGE = 64

_MATRIX_XML = {
    "matrix-1x1": (257, 257, 66049, "f0e4eae669c1b1b47eb4877445395db5f37ad29cba3abc568e4e9863767c4326"),
    "matrix-4x1": (509, 131, 66679, "e5586d5b9676c0c4179d09c1b26f2b868a1e395c840593c4d296994eef738afe"),
}


@dataclass(frozen=True, slots=True)
class OrderedDitherMatrix:
    width: int
    height: int
    thresholds: tuple[int, ...]
    source: str

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError("ordered matrix dimensions must be positive")
        if len(self.thresholds) != self.width * self.height:
            raise ValueError("ordered matrix sample count does not match its dimensions")
        if any(not 0 <= threshold <= UINT16_MAX for threshold in self.thresholds):
            raise ValueError("ordered matrix thresholds must be uint16 values")

    def threshold(self, x: int, y: int, *, x_offset: int = 0, y_offset: int = 0) -> int:
        mx = (x + x_offset) % self.width
        my = (y + y_offset) % self.height
        return self.thresholds[my * self.width + mx]

    def transposed(self) -> OrderedDitherMatrix:
        """Exchange axes while preserving the verified threshold samples."""

        return OrderedDitherMatrix(
            width=self.height,
            height=self.width,
            thresholds=tuple(
                self.thresholds[y * self.width + x]
                for x in range(self.width) for y in range(self.height)
            ),
            source=self.source + " (transposed)",
        )


@dataclass(frozen=True, slots=True)
class _DropEndpoint:
    value: int
    bits: int


@dataclass(frozen=True, slots=True)
class GutenprintDropModel:
    """Variable-dot endpoints constructed like ``stp_dither_set_inks``."""

    drop_sizes: tuple[float, ...]
    endpoints: tuple[_DropEndpoint, ...]
    raster_bit_depth: int | None = None

    @classmethod
    def from_drop_sizes(
        cls, drop_sizes: tuple[float, ...], *, raster_bit_depth: int | None = None
    ) -> GutenprintDropModel:
        # Gutenprint skips zero-sized drops but keeps each original bit_pattern
        # (dither-inks.c: stp_dither_set_inks). A lone DropSize3 is code 3.
        active = tuple(
            (index, value)
            for index, value in enumerate(drop_sizes, start=1)
            if value > 0
        )
        if not active:
            raise ValueError("at least one positive drop size is required")
        if any(value > 1 for _, value in active):
            raise ValueError("this proven Ordered subset requires normalized drop sizes <= 1")
        endpoints = [_DropEndpoint(0, 0)]
        for index, value in active:
            endpoints.append(_DropEndpoint(min(int(value * UINT16_MAX), UINT16_MAX), index))
        if any(left.value >= right.value for left, right in pairwise(endpoints)):
            raise ValueError("positive drop sizes must be strictly increasing")
        minimum_bits = max(endpoint.bits for endpoint in endpoints).bit_length()
        if raster_bit_depth is not None and not minimum_bits <= raster_bit_depth <= 4:
            raise ValueError("raster bit depth cannot encode the active drop codes")
        return cls(
            drop_sizes=drop_sizes,
            endpoints=tuple(endpoints),
            raster_bit_depth=raster_bit_depth,
        )

    @property
    def bit_depth(self) -> int:
        return self.raster_bit_depth or max(endpoint.bits for endpoint in self.endpoints).bit_length()

    def sample_code(self, value: int, threshold: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= UINT16_MAX:
            raise ValueError("Ordered input must be an integer uint16 value")
        if not 0 <= threshold <= UINT16_MAX:
            raise ValueError("Ordered threshold must be uint16")
        for lower, upper in reversed(tuple(zip(self.endpoints, self.endpoints[1:]))):
            if value > lower.value:
                value_span = upper.value - lower.value
                range_point = value - lower.value
                if value_span < UINT16_MAX:
                    range_point = range_point * UINT16_MAX // value_span
                return upper.bits if range_point >= threshold else lower.bits
        return 0


@dataclass(frozen=True, slots=True)
class GutenprintOrderedDither:
    matrix: OrderedDitherMatrix
    drops: GutenprintDropModel
    x_offset: int = 0
    y_offset: int = 0

    def dither_placed(
        self,
        plane: LogicalContonePlane,
        *,
        canvas_width: int,
        canvas_height: int,
        x: int,
        y: int,
    ) -> LogicalDotPlane:
        """Dither a logical plane at exact global matrix coordinates."""

        if canvas_width <= 0 or canvas_width % 8:
            raise ValueError("dither canvas width must be a positive multiple of 8")
        if canvas_height <= 0:
            raise ValueError("dither canvas height must be positive")
        if x < 0 or y < 0 or x + plane.width_pixels > canvas_width:
            raise ValueError("logical plane exceeds horizontal dither canvas bounds")
        if y + plane.height_rows > canvas_height:
            raise ValueError("logical plane exceeds vertical dither canvas bounds")
        if plane.width_pixels * plane.height_rows < _CYTHON_MIN_PIXELS:
            return _numpy_dither_placed(
                self,
                plane,
                canvas_width=canvas_width,
                canvas_height=canvas_height,
                x=x,
                y=y,
            )
        from xp750_rip._cython_kernels import ordered_band_kernel

        bit_depth = self.drops.bit_depth
        row_bytes = canvas_width // 8
        packed = np.zeros((bit_depth, canvas_height, row_bytes), dtype=np.uint8)
        matrix = np.asarray(self.matrix.thresholds, dtype=np.uint16).reshape(
            self.matrix.height, self.matrix.width
        )
        lower_bits, upper_bits, range_points = _drop_lookup(self.drops.endpoints)

        # Ограниченный band сохраняет bounded-memory contract. AOT Cython
        # объединяет matrix lookup, drop selection и bit packing в один
        # parallel row-loop без JIT и full-image temporaries.
        band_rows = 128
        for local_start in range(0, plane.height_rows, band_rows):
            local_end = min(local_start + band_rows, plane.height_rows)
            values = np.asarray(plane.rows[local_start:local_end], dtype=np.uint16)
            global_start = y + local_start
            global_end = y + local_end
            packed[:, global_start:global_end] = ordered_band_kernel(
                values,
                matrix,
                lower_bits,
                upper_bits,
                range_points,
                canvas_width,
                x,
                global_start,
                self.x_offset,
                self.y_offset,
                bit_depth,
            )
        return LogicalDotPlane(
            identity=plane.identity,
            width_pixels=canvas_width,
            bit_plane_rows=tuple(
                tuple(row.tobytes() for row in bit_plane) for bit_plane in packed
            ),
        )

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
        """Production fused exact QTR lookup and Ordered dither path.

        The retained ``dither_placed`` plus ``execute_runtime_curve_codes``
        path remains the readable semantic reference.  This method changes
        only execution shape: the same integer LUT and Ordered formula are
        applied without materializing a full uint16 contone plane.
        """

        if not code_rows or not code_rows[0]:
            raise ValueError("QTR code raster dimensions must be positive")
        width = len(code_rows[0])
        height = len(code_rows)
        if getattr(code_rows, "width", None) != width and any(len(row) != width for row in code_rows):
            raise ValueError("all QTR code rows must share width")
        if len(qtr_lut) not in (256, 65536) or any(not 0 <= value <= UINT16_MAX for value in qtr_lut):
            raise ValueError("QTR lookup table must contain 256 or 65536 uint16 values")
        if canvas_width <= 0 or canvas_width % 8:
            raise ValueError("dither canvas width must be a positive multiple of 8")
        if canvas_height <= 0 or x < 0 or y < 0:
            raise ValueError("invalid dither canvas placement")
        if x + width > canvas_width or y + height > canvas_height:
            raise ValueError("QTR code raster exceeds dither canvas bounds")

        from xp750_rip._cython_kernels import ordered_qtr_band_kernel

        bit_depth = self.drops.bit_depth
        row_bytes = canvas_width // 8
        packed = np.zeros((bit_depth, canvas_height, row_bytes), dtype=np.uint8)
        matrix = np.asarray(self.matrix.thresholds, dtype=np.uint16).reshape(
            self.matrix.height, self.matrix.width
        )
        lut = np.asarray(qtr_lut, dtype=np.uint16)
        lower_bits, upper_bits, range_points = _drop_lookup(self.drops.endpoints)
        band_rows = 128
        for local_start in range(0, height, band_rows):
            local_end = min(local_start + band_rows, height)
            codes = np.frombuffer(
                b"".join(code_rows[local_start:local_end]), dtype=np.uint16 if len(qtr_lut) == 65536 else np.uint8
            ).reshape(local_end - local_start, width)
            global_start = y + local_start
            global_end = y + local_end
            packed[:, global_start:global_end] = ordered_qtr_band_kernel(
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
                bit_depth,
            )
        return LogicalDotPlane(
            identity=identity,
            width_pixels=canvas_width,
            bit_plane_rows=tuple(
                tuple(row.tobytes() for row in bit_plane) for bit_plane in packed
            ),
        )


def _numpy_dither_placed(
    dither: GutenprintOrderedDither,
    plane: LogicalContonePlane,
    *,
    canvas_width: int,
    canvas_height: int,
    x: int,
    y: int,
) -> LogicalDotPlane:
    """Retained bounded-memory NumPy path for small rasters and reference tests."""

    bit_depth = dither.drops.bit_depth
    row_bytes = canvas_width // 8
    packed = np.zeros((bit_depth, canvas_height, row_bytes), dtype=np.uint8)
    matrix = np.asarray(dither.matrix.thresholds, dtype=np.uint16).reshape(
        dither.matrix.height, dither.matrix.width
    )
    matrix_x = (
        np.arange(x, x + plane.width_pixels, dtype=np.int64) + dither.x_offset
    ) % dither.matrix.width
    lower_bits, upper_bits, range_points = _drop_lookup(dither.drops.endpoints)
    band_rows = 128
    for local_start in range(0, plane.height_rows, band_rows):
        local_end = min(local_start + band_rows, plane.height_rows)
        values = np.asarray(plane.rows[local_start:local_end], dtype=np.uint16)
        global_start = y + local_start
        global_end = y + local_end
        matrix_y = (
            np.arange(global_start, global_end, dtype=np.int64) + dither.y_offset
        ) % dither.matrix.height
        thresholds = matrix[np.ix_(matrix_y, matrix_x)]
        codes = np.where(
            range_points[values] >= thresholds,
            upper_bits[values],
            lower_bits[values],
        )
        for bit_index in range(bit_depth):
            canvas_bits = np.zeros((local_end - local_start, canvas_width), dtype=np.bool_)
            canvas_bits[:, x : x + plane.width_pixels] = (
                codes & (1 << bit_index)
            ) != 0
            packed[bit_index, global_start:global_end] = np.packbits(
                canvas_bits, axis=1, bitorder="big"
            )
    return LogicalDotPlane(
        identity=plane.identity,
        width_pixels=canvas_width,
        bit_plane_rows=tuple(
            tuple(row.tobytes() for row in bit_plane) for bit_plane in packed
        ),
    )


@lru_cache(maxsize=16)
def _drop_lookup(
    endpoints: tuple[_DropEndpoint, ...],
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Compile exact scalar ``sample_code`` intervals into three uint LUTs."""

    lower_bits = np.zeros(UINT16_MAX + 1, dtype=np.uint16)
    upper_bits = np.zeros(UINT16_MAX + 1, dtype=np.uint16)
    range_points = np.zeros(UINT16_MAX + 1, dtype=np.uint32)
    pair_count = len(endpoints) - 1
    for index, (lower, upper) in enumerate(pairwise(endpoints)):
        first = lower.value + 1
        last = upper.value if index < pair_count - 1 else UINT16_MAX
        if first > last:
            continue
        values = np.arange(first, last + 1, dtype=np.uint32)
        points = values - lower.value
        span = upper.value - lower.value
        if span < UINT16_MAX:
            points = points * UINT16_MAX // span
        lower_bits[first : last + 1] = lower.bits
        upper_bits[first : last + 1] = upper.bits
        range_points[first : last + 1] = points
    for lookup in (lower_bits, upper_bits, range_points):
        lookup.setflags(write=False)
    return lower_bits, upper_bits, range_points


def unpack_sample_codes(plane: LogicalDotPlane) -> tuple[bytes, ...]:
    """Expand packed bit planes to one 0..(2**depth-1) byte per pixel."""

    rows: list[bytes] = []
    for row_index in range(plane.height_rows):
        values = bytearray(plane.width_pixels)
        for x in range(plane.width_pixels):
            byte_index = x // 8
            bit = 0x80 >> (x % 8)
            code = 0
            for bit_index, bit_plane in enumerate(plane.bit_plane_rows):
                if bit_plane[row_index][byte_index] & bit:
                    code |= 1 << bit_index
            values[x] = code
        rows.append(bytes(values))
    return tuple(rows)


@lru_cache(maxsize=1)
def load_gutenprint_matrix_2x1() -> OrderedDitherMatrix:
    """Load the exact 367x179 matrix sequence vendored from Gutenprint 5.3.5."""

    data_dir = files("xp750_rip.dither").joinpath("data")
    parts = sorted(
        item for item in data_dir.iterdir() if item.name.startswith("matrix_2x1_")
    )
    thresholds = tuple(
        int(token)
        for part in parts
        for token in part.read_text(encoding="ascii").split()
    )
    if len(thresholds) != _MATRIX_2X1_COUNT:
        raise RuntimeError("vendored Gutenprint matrix-2x1 has an unexpected sample count")
    packed = b"".join(value.to_bytes(2, "little") for value in thresholds)
    if sha256(packed).hexdigest() != _MATRIX_2X1_U16LE_SHA256:
        raise RuntimeError("vendored Gutenprint matrix-2x1 failed its source hash check")
    return OrderedDitherMatrix(
        width=_MATRIX_2X1_WIDTH,
        height=_MATRIX_2X1_HEIGHT,
        thresholds=thresholds,
        source="Gutenprint 5.3.5 src/xml/dither/matrix-2x1.xml",
    )


@lru_cache(maxsize=1)
def load_gutenprint_fast_matrix() -> OrderedDitherMatrix:
    """Build Gutenprint's exact 64x64 ``Fast``/``VeryFast`` matrix.

    Gutenprint creates this matrix at runtime by iterating the 2x2 Bayer
    seed six times and then applying x/y shears 2 and 4.  Keeping the small
    generator here is easier to audit than vendoring another 4096 numbers.
    """

    seed = (0, 2, 3, 1)

    def point(x: int, y: int) -> int:
        value = 0
        divisor = 1
        for iteration in range(6):
            base = seed[(y // divisor) % 2 + ((x // divisor) % 2) * 2]
            value += base * (4 ** (5 - iteration))
            divisor *= 2
        # The C implementation converts through double and truncates.
        return int(value * 65536.0 / (_FAST_MATRIX_EDGE * _FAST_MATRIX_EDGE))

    # print-dither-matrices.c applies x_shear first and y_shear second.
    thresholds = tuple(
        point((x * 5) % _FAST_MATRIX_EDGE, (y * 3) % _FAST_MATRIX_EDGE)
        for y in range(_FAST_MATRIX_EDGE)
        for x in range(_FAST_MATRIX_EDGE)
    )
    return OrderedDitherMatrix(
        width=_FAST_MATRIX_EDGE,
        height=_FAST_MATRIX_EDGE,
        thresholds=thresholds,
        source=(
            "Gutenprint 5.3.5 runtime iterated Bayer matrix: "
            "edge=2, iterations=6, x_shear=2, y_shear=4"
        ),
    )


@lru_cache(maxsize=4)
def load_gutenprint_matrix(matrix_id: str) -> OrderedDitherMatrix:
    """Load the exact standard matrix selected by Gutenprint for a DPI aspect."""

    if matrix_id == "matrix-1x2":
        return load_gutenprint_matrix_2x1().transposed()
    if matrix_id == "matrix-2x1":
        return load_gutenprint_matrix_2x1()
    try:
        width, height, count, expected_hash = _MATRIX_XML[matrix_id]
    except KeyError as error:
        raise ValueError(f"Неизвестная Gutenprint Ordered matrix: {matrix_id}") from error
    resource = files("xp750_rip.dither").joinpath("data", f"{matrix_id}.xml")
    root = ElementTree.fromstring(resource.read_text(encoding="utf-8"))
    namespace = {"g": "http://gimp-print.sourceforge.net/xsd/gp.xsd-1.0"}
    sequence = root.find("g:dither-matrix/g:array/g:sequence", namespace)
    if sequence is None or sequence.text is None:
        raise RuntimeError(f"vendored Gutenprint {matrix_id} has no sequence")
    thresholds = tuple(int(token) for token in sequence.text.split())
    packed = b"".join(value.to_bytes(2, "little") for value in thresholds)
    if len(thresholds) != count or sha256(packed).hexdigest() != expected_hash:
        raise RuntimeError(f"vendored Gutenprint {matrix_id} failed source hash check")
    return OrderedDitherMatrix(
        width=width,
        height=height,
        thresholds=thresholds,
        source=f"Gutenprint 5.3.5 src/xml/dither/{matrix_id}.xml",
    )
