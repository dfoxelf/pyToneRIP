# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Generic immutable N-channel continuous-tone raster containers."""

from __future__ import annotations

from array import array
from collections.abc import Sequence
from dataclasses import dataclass

UINT16_MAX = 65535


@dataclass(frozen=True, slots=True, order=True)
class LogicalChannelId:
    """Identity of one logical output channel, independent of printer paths."""

    value: str

    def __post_init__(self) -> None:
        if not self.value or self.value.strip() != self.value:
            raise ValueError("logical channel identity must be a non-empty trimmed string")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class LogicalContonePlane:
    """One uint16 logical-channel plane with explicit identity."""

    identity: LogicalChannelId
    width_pixels: int
    rows: tuple[Sequence[int], ...]

    def __post_init__(self) -> None:
        if self.width_pixels <= 0:
            raise ValueError("contone width must be positive")
        if not self.rows:
            raise ValueError("contone height must be positive")
        if any(len(row) != self.width_pixels for row in self.rows):
            raise ValueError("every contone row must match the declared width")
        if any(not _is_valid_uint16_row(row) for row in self.rows):
            raise ValueError("contone samples must be integer uint16 values")

    @property
    def height_rows(self) -> int:
        return len(self.rows)


def _is_valid_uint16_row(row: Sequence[int]) -> bool:
    if isinstance(row, array) and row.typecode == "H" and row.itemsize == 2:
        return True
    return all(
        not isinstance(sample, bool)
        and isinstance(sample, int)
        and 0 <= sample <= UINT16_MAX
        for sample in row
    )


@dataclass(frozen=True, slots=True)
class LogicalContoneRasterSet:
    """N explicitly identified logical uint16 planes with common geometry."""

    planes: tuple[LogicalContonePlane, ...]

    def __post_init__(self) -> None:
        if not self.planes:
            raise ValueError("logical contone raster set must contain at least one plane")
        identities = tuple(plane.identity for plane in self.planes)
        if len(identities) != len(set(identities)):
            raise ValueError("logical contone identities must be unique")
        geometry = (self.planes[0].width_pixels, self.planes[0].height_rows)
        if any((plane.width_pixels, plane.height_rows) != geometry for plane in self.planes[1:]):
            raise ValueError("all logical contone planes must share geometry")

    @property
    def width_pixels(self) -> int:
        return self.planes[0].width_pixels

    @property
    def height_rows(self) -> int:
        return self.planes[0].height_rows

    @property
    def identities(self) -> tuple[LogicalChannelId, ...]:
        return tuple(plane.identity for plane in self.planes)

    def get(self, identity: LogicalChannelId) -> LogicalContonePlane:
        for plane in self.planes:
            if plane.identity == identity:
                return plane
        raise KeyError(identity)
