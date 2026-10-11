# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Explicit fixed-mode raster passes for the Phase 3C model-96 experiment."""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from xp750_rip.escp2 import commands
from xp750_rip.escp2.channels import BLACK_INK_ADDRESS, Escp2InkAddress
from xp750_rip.escp2.packbits import encode as packbits_encode


@dataclass(frozen=True, slots=True)
class RasterPass:
    """One fixed-mechanics, two-plane, PackBits-compressed 180-row pass."""

    pass_index: int
    vertical_advance: int
    raster_width_pixels: int
    plane0_rows: tuple[bytes, ...]
    plane1_rows: tuple[bytes, ...]
    address: Escp2InkAddress = BLACK_INK_ADDRESS
    bit_depth: int = field(init=False, default=2)
    compression: int = field(init=False, default=1)
    encoded_rows: tuple[bytes, ...] | None = field(
        default=None, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if self.raster_width_pixels <= 0 or self.raster_width_pixels % 8:
            raise ValueError("raster width must be a positive multiple of 8 pixels")
        if not 1 <= len(self.plane0_rows) <= 180 or len(self.plane1_rows) != len(
            self.plane0_rows
        ):
            raise ValueError("Model-96 raster pass должен содержать 1..180 rows per plane")
        plane_bytes = self.bytes_per_plane_row
        if any(len(row) != plane_bytes for row in self.plane0_rows + self.plane1_rows):
            raise ValueError("every bit-plane row must match the declared raster width")
        if self.encoded_rows is not None and len(self.encoded_rows) != self.row_count:
            raise ValueError("cached PackBits row count must match the raster pass")

    @property
    def row_count(self) -> int:
        return len(self.plane0_rows)

    @property
    def selector(self) -> int:
        return self.address.selector

    @property
    def bytes_per_plane_row(self) -> int:
        return self.raster_width_pixels // 8

    @property
    def bytes_per_raster_row(self) -> int:
        return self.bytes_per_plane_row * self.bit_depth

    @property
    def decoded_rows(self) -> tuple[bytes, ...]:
        return tuple(
            plane0 + plane1
            for plane0, plane1 in zip(self.plane0_rows, self.plane1_rows, strict=True)
        )

    @property
    def compressed_rows(self) -> tuple[bytes, ...]:
        if self.encoded_rows is not None:
            return self.encoded_rows
        return tuple(packbits_encode(row) for row in self.decoded_rows)

    def with_compressed_rows(self) -> RasterPass:
        if self.encoded_rows is not None:
            return self
        return replace(self, encoded_rows=tuple(packbits_encode(row) for row in self.decoded_rows))

    @property
    def compressed_payload(self) -> bytes:
        return b"".join(self.compressed_rows)

    def encode_raster_record(self) -> bytes:
        """Encode ``ESC i`` payload and its terminating CR, without paper feed."""

        output = bytearray(
            commands.addressed_raster_header(
                address=self.address,
                compression=self.compression,
                bit_depth=self.bit_depth,
                bytes_per_row=self.bytes_per_raster_row,
                rows=self.row_count,
            )
        )
        output.extend(self.compressed_payload)
        output.extend(commands.carriage_return())
        return bytes(output)

    def encode(self) -> bytes:
        output = bytearray(commands.relative_vertical(self.vertical_advance))
        output.extend(self.encode_raster_record())
        return bytes(output)
