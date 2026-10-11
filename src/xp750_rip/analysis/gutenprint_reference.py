# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Верификация файла-эталона и измерения декодированного растра."""

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from xp750_rip.analysis.escp2_parser import RasterData, StreamRecord, parse_stream


@dataclass(frozen=True, slots=True)
class ReferenceVerification:
    path: Path
    byte_length: int
    sha256: str
    first_256_bytes: bytes
    records: tuple[StreamRecord, ...]


@dataclass(frozen=True, slots=True)
class RasterPlaneSummary:
    plane: int
    active_bits: int
    bounding_box: tuple[int, int, int, int] | None


@dataclass(frozen=True, slots=True)
class RasterSummary:
    pixels_per_row: int
    rows: int
    planes: tuple[RasterPlaneSummary, ...]


def verify_reference(path: Path, expected_sha256: str | None = None) -> ReferenceVerification:
    """Прочитать и разобрать любой локальный PRN; при необходимости проверить SHA-256."""

    data = path.read_bytes()
    digest = sha256(data).hexdigest()
    if expected_sha256 is not None and digest.lower() != expected_sha256.lower():
        raise ValueError(f"SHA-256 не совпадает: ожидался {expected_sha256}, получен {digest}")
    return ReferenceVerification(
        path=path,
        byte_length=len(data),
        sha256=digest,
        first_256_bytes=data[:256],
        records=tuple(parse_stream(data)),
    )


def summarize_raster(raster: RasterData) -> RasterSummary:
    """Подсчитать активные точки и рамку отдельно для каждой битовой плоскости."""

    if raster.bit_depth <= 0 or raster.bytes_per_row % raster.bit_depth:
        raise ValueError("число байт строки нельзя равномерно разделить на битовые плоскости")
    plane_bytes = raster.bytes_per_row // raster.bit_depth
    planes: list[RasterPlaneSummary] = []
    for plane in range(raster.bit_depth):
        active_bits = 0
        min_x: int | None = None
        min_y: int | None = None
        max_x: int | None = None
        max_y: int | None = None
        for y, row in enumerate(raster.decoded_rows):
            plane_row = row[plane * plane_bytes : (plane + 1) * plane_bytes]
            active_bits += sum(value.bit_count() for value in plane_row)
            for byte_x, value in enumerate(plane_row):
                if value == 0:
                    continue
                for bit in range(8):
                    if value & (0x80 >> bit):
                        x = byte_x * 8 + bit
                        min_x = x if min_x is None else min(min_x, x)
                        min_y = y if min_y is None else min(min_y, y)
                        max_x = x if max_x is None else max(max_x, x)
                        max_y = y if max_y is None else max(max_y, y)
        bounding_box = None
        if min_x is not None and min_y is not None and max_x is not None and max_y is not None:
            bounding_box = (min_x, min_y, max_x, max_y)
        planes.append(RasterPlaneSummary(plane, active_bits, bounding_box))
    pixels_per_row = raster.pixels_per_row
    if pixels_per_row is None:
        raise ValueError("ширина растра в пикселях не определена")
    return RasterSummary(pixels_per_row, raster.rows, tuple(planes))
