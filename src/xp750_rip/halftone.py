# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Generic printer-neutral logical dot-plane representation."""

from dataclasses import dataclass

from xp750_rip.contone import LogicalChannelId


@dataclass(frozen=True, slots=True)
class LogicalDotPlane:
    """Packed LSB-first sample bit planes for one logical channel."""

    identity: LogicalChannelId
    width_pixels: int
    bit_plane_rows: tuple[tuple[bytes, ...], ...]

    def __post_init__(self) -> None:
        if self.width_pixels <= 0 or self.width_pixels % 8:
            raise ValueError("dot-plane width must be a positive multiple of 8 pixels")
        if not self.bit_plane_rows:
            raise ValueError("dot plane must contain at least one sample bit plane")
        height = len(self.bit_plane_rows[0])
        row_bytes = self.width_pixels // 8
        if height <= 0:
            raise ValueError("dot-plane height must be positive")
        for bit_plane in self.bit_plane_rows:
            if len(bit_plane) != height:
                raise ValueError("all dot bit planes must share height")
            if any(len(row) != row_bytes for row in bit_plane):
                raise ValueError("packed dot rows must match the declared width")

    @property
    def height_rows(self) -> int:
        return len(self.bit_plane_rows[0])

    @property
    def bit_depth(self) -> int:
        return len(self.bit_plane_rows)

    @property
    def active_bits(self) -> tuple[int, ...]:
        return tuple(
            sum(byte.bit_count() for row in bit_plane for byte in row)
            for bit_plane in self.bit_plane_rows
        )
