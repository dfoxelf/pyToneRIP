# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Source-derived ESC/P2 ink addressing for model-96 raster commands."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Escp2InkAddress:
    """The separate Gutenprint ``color`` and ``subchannel`` address fields.

    ``stpi_escp2_flush_pass`` combines these fields only at serialization time
    as ``color | (subchannel << 4)`` and writes the result into ``ESC i``.
    """

    color: int
    subchannel: int

    def __post_init__(self) -> None:
        if not 0 <= self.color <= 0x0F:
            raise ValueError("ESC/P2 color must fit the low nibble")
        if not 0 <= self.subchannel <= 0x0F:
            raise ValueError("ESC/P2 subchannel must fit the high nibble")

    @property
    def selector(self) -> int:
        return self.color | (self.subchannel << 4)

    @classmethod
    def from_selector(cls, selector: int) -> "Escp2InkAddress":
        if not 0 <= selector <= 0xFF:
            raise ValueError("ESC/P2 selector must fit an unsigned byte")
        return cls(color=selector & 0x0F, subchannel=selector >> 4)


BLACK_INK_ADDRESS = Escp2InkAddress(color=0, subchannel=0)
