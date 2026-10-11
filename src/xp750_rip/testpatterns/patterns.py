# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Generate direct one-channel binary patterns without color conversion."""

from dataclasses import asdict, dataclass
from enum import StrEnum


@dataclass(frozen=True, slots=True)
class PatternGeometry:
    width_pixels: int
    height_pixels: int
    rectangle_mm: float
    line_width_pixels: int

    def as_dict(self) -> dict[str, int | float]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Phase3CPatternGeometry:
    logical_width_pixels: int
    logical_height_rows: int
    bounds: tuple[int, int, int, int]
    line_width_pixels: int
    line_width_rows: int
    expected_width_mm: float
    expected_height_mm: float

    def as_dict(self) -> dict[str, int | float | list[int]]:
        return {
            "logical_width_pixels": self.logical_width_pixels,
            "logical_height_rows": self.logical_height_rows,
            "bounds": list(self.bounds),
            "line_width_pixels": self.line_width_pixels,
            "line_width_rows": self.line_width_rows,
            "expected_width_mm": self.expected_width_mm,
            "expected_height_mm": self.expected_height_mm,
        }


class IdentificationPattern(StrEnum):
    """Low-coverage marks used to distinguish independent physical rasters."""

    VERTICAL_LINE = "vertical_line"
    HORIZONTAL_LINE = "horizontal_line"
    BACKSLASH = "diagonal_backslash"
    SLASH = "diagonal_slash"
    RECTANGLE = "rectangle"
    CROSS = "cross"


@dataclass(frozen=True, slots=True)
class MultipathPatternGeometry:
    kind: IdentificationPattern
    bounds: tuple[int, int, int, int]
    active_bounds: tuple[int, int, int, int]
    line_width_pixels: int
    line_width_rows: int
    cell_width_mm: float
    cell_height_mm: float

    def as_dict(self) -> dict[str, str | int | float | list[int]]:
        return {
            "kind": self.kind.value,
            "cell_bounds": list(self.bounds),
            "active_bounds": list(self.active_bounds),
            "line_width_pixels": self.line_width_pixels,
            "line_width_rows": self.line_width_rows,
            "cell_width_mm": self.cell_width_mm,
            "cell_height_mm": self.cell_height_mm,
        }


def make_phase2_pattern(
    horizontal_dpi: int, vertical_dpi: int
) -> tuple[list[bytes], PatternGeometry]:
    """Return rows of 0/1 pixels for an outline plus center cross."""
    width = round(10 * horizontal_dpi / 25.4)
    height = round(10 * vertical_dpi / 25.4)
    width = (width + 7) // 8 * 8
    pixels = [[False] * width for _ in range(height)]
    for x in range(width):
        pixels[0][x] = pixels[-1][x] = True
    for y in range(height):
        pixels[y][0] = pixels[y][-1] = True
    for x in range(width):
        pixels[height // 2][x] = True
    for y in range(height):
        pixels[y][width // 2] = True

    rows: list[bytes] = []
    for row in pixels:
        packed = bytearray(width // 8)
        for x, active in enumerate(row):
            if active:
                packed[x // 8] |= 0x80 >> (x % 8)
        rows.append(bytes(packed))
    return rows, PatternGeometry(width, height, 10.0, 1)


def _set_horizontal_span(row: bytearray, start: int, end: int) -> None:
    for x in range(start, end + 1):
        row[x // 8] |= 0x80 >> (x % 8)


def make_phase3c_pattern(
    *,
    logical_width_pixels: int,
    logical_height_rows: int,
    bounds: tuple[int, int, int, int],
    horizontal_dpi: int,
    vertical_dpi: int,
    nozzle_separation: int,
) -> tuple[tuple[bytes, ...], Phase3CPatternGeometry]:
    """Generate an outlined square with two diagonals without reference raster data."""

    if logical_width_pixels % 8:
        raise ValueError("logical raster width must be byte aligned")
    x0, y0, x1, y1 = bounds
    if not (0 <= x0 < x1 < logical_width_pixels and 0 <= y0 < y1 < logical_height_rows):
        raise ValueError("pattern bounds must lie inside the logical raster")

    line_width_pixels = 8
    line_width_rows = 2
    rows = [bytearray(logical_width_pixels // 8) for _ in range(logical_height_rows)]
    pattern_width = x1 - x0 + 1
    pattern_height = y1 - y0 + 1
    for y in range(y0, y1 + 1):
        row = rows[y]
        local_y = y - y0
        if local_y < line_width_rows or y1 - y < line_width_rows:
            _set_horizontal_span(row, x0, x1)
            continue

        _set_horizontal_span(row, x0, x0 + line_width_pixels - 1)
        _set_horizontal_span(row, x1 - line_width_pixels + 1, x1)
        diagonal = round(local_y * (pattern_width - 1) / (pattern_height - 1))
        for center in (x0 + diagonal, x1 - diagonal):
            start = max(x0, center - line_width_pixels // 2)
            end = min(x1, start + line_width_pixels - 1)
            start = max(x0, end - line_width_pixels + 1)
            _set_horizontal_span(row, start, end)

    width_mm = pattern_width / horizontal_dpi * 25.4
    height_mm = pattern_height * nozzle_separation / vertical_dpi * 25.4
    geometry = Phase3CPatternGeometry(
        logical_width_pixels=logical_width_pixels,
        logical_height_rows=logical_height_rows,
        bounds=bounds,
        line_width_pixels=line_width_pixels,
        line_width_rows=line_width_rows,
        expected_width_mm=round(width_mm, 3),
        expected_height_mm=round(height_mm, 3),
    )
    return tuple(bytes(row) for row in rows), geometry


def _set_pixel_span(row: bytearray, center: int, width: int, x0: int, x1: int) -> None:
    start = max(x0, center - width // 2)
    end = min(x1, start + width - 1)
    start = max(x0, end - width + 1)
    _set_horizontal_span(row, start, end)


def _active_bounds(rows: list[bytearray]) -> tuple[int, int, int, int]:
    active_x: list[int] = []
    active_y: list[int] = []
    for y, row in enumerate(rows):
        for byte_index, value in enumerate(row):
            if not value:
                continue
            for bit_index in range(8):
                if value & (0x80 >> bit_index):
                    active_x.append(byte_index * 8 + bit_index)
                    active_y.append(y)
    if not active_x:
        raise ValueError("identification pattern unexpectedly contains no active pixels")
    return min(active_x), min(active_y), max(active_x), max(active_y)


def make_identification_pattern(
    *,
    kind: IdentificationPattern,
    logical_width_pixels: int,
    logical_height_rows: int,
    bounds: tuple[int, int, int, int],
    horizontal_dpi: int,
    vertical_dpi: int,
    nozzle_separation: int,
) -> tuple[tuple[bytes, ...], MultipathPatternGeometry]:
    """Generate one deterministic low-coverage path-identification mark."""

    if logical_width_pixels <= 0 or logical_width_pixels % 8:
        raise ValueError("logical raster width must be a positive multiple of 8")
    if logical_height_rows <= 0:
        raise ValueError("logical raster height must be positive")
    x0, y0, x1, y1 = bounds
    if not (0 <= x0 < x1 < logical_width_pixels and 0 <= y0 < y1 < logical_height_rows):
        raise ValueError("pattern bounds must lie inside the logical raster")

    line_width_pixels = 4
    line_width_rows = 4
    rows = [bytearray(logical_width_pixels // 8) for _ in range(logical_height_rows)]
    pattern_width = x1 - x0 + 1
    pattern_height = y1 - y0 + 1
    center_x = (x0 + x1) // 2
    center_y = (y0 + y1) // 2

    if kind is IdentificationPattern.VERTICAL_LINE:
        for y in range(y0, y1 + 1):
            _set_pixel_span(rows[y], center_x, line_width_pixels, x0, x1)
    elif kind is IdentificationPattern.HORIZONTAL_LINE:
        for y in range(center_y - 1, center_y + 3):
            _set_horizontal_span(rows[y], x0, x1)
    elif kind in {IdentificationPattern.BACKSLASH, IdentificationPattern.SLASH}:
        for y in range(y0, y1 + 1):
            local_y = y - y0
            diagonal = round(local_y * (pattern_width - 1) / (pattern_height - 1))
            center = x0 + diagonal
            if kind is IdentificationPattern.SLASH:
                center = x1 - diagonal
            _set_pixel_span(rows[y], center, line_width_pixels, x0, x1)
    elif kind is IdentificationPattern.RECTANGLE:
        for y in range(y0, y1 + 1):
            if y - y0 < 2 or y1 - y < 2:
                _set_horizontal_span(rows[y], x0, x1)
            else:
                _set_horizontal_span(rows[y], x0, x0 + line_width_pixels - 1)
                _set_horizontal_span(rows[y], x1 - line_width_pixels + 1, x1)
    elif kind is IdentificationPattern.CROSS:
        for y in range(y0, y1 + 1):
            _set_pixel_span(rows[y], center_x, line_width_pixels, x0, x1)
        for y in range(center_y - 1, center_y + 3):
            _set_horizontal_span(rows[y], x0, x1)
    else:  # pragma: no cover - StrEnum exhaustiveness guard
        raise ValueError(f"unsupported identification pattern: {kind}")

    geometry = MultipathPatternGeometry(
        kind=kind,
        bounds=bounds,
        active_bounds=_active_bounds(rows),
        line_width_pixels=line_width_pixels,
        line_width_rows=line_width_rows,
        cell_width_mm=round(pattern_width / horizontal_dpi * 25.4, 3),
        cell_height_mm=round(
            pattern_height * nozzle_separation / vertical_dpi * 25.4,
            3,
        ),
    )
    return tuple(bytes(row) for row in rows), geometry
