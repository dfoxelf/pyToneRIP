# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Offline ESC/P-R framing and input-page geometry, separate from ESC/P2.

Field meanings follow Epson's published escpr_def.h (ESCPR_PRINT_JOB),
checked against local Windows driver captures. This decoder does not infer
physical nozzle resolution, drop sizes or firmware weave from input DPI.
Unknown packets and extended quality payloads remain raw evidence.
"""

from dataclasses import dataclass
from struct import unpack

ESCPR_ENTRY = b"\x1b(R\x06\x00\x00ESCPR"
_INPUT_DPI = {0: 360, 1: 720, 2: 300, 3: 600}


@dataclass(frozen=True, slots=True)
class EscprPageGeometry:
    paper_width: int
    paper_height: int
    top_margin: int
    left_margin: int
    printable_width: int
    printable_height: int
    input_resolution_code: int
    print_direction_code: int

    @property
    def input_dpi(self) -> int | None:
        return _INPUT_DPI.get(self.input_resolution_code)

    @property
    def paper_size_mm(self) -> tuple[float, float] | None:
        dpi = self.input_dpi
        if dpi is None:
            return None
        return self.paper_width * 25.4 / dpi, self.paper_height * 25.4 / dpi

    @property
    def margins_mm(self) -> tuple[float, float, float, float] | None:
        """Left, top, right, bottom in physical millimetres."""
        dpi = self.input_dpi
        if dpi is None:
            return None
        values = (self.left_margin, self.top_margin,
                  self.paper_width - self.left_margin - self.printable_width,
                  self.paper_height - self.top_margin - self.printable_height)
        return tuple(value * 25.4 / dpi for value in values)


@dataclass(frozen=True, slots=True)
class EscprPacket:
    offset: int
    end_offset: int
    command: str
    name: str
    payload: bytes

    @property
    def page_geometry(self) -> EscprPageGeometry | None:
        if self.name != "setj" or len(self.payload) != 22:
            return None
        return EscprPageGeometry(*unpack(">IIhhIIBB", self.payload))


def parse_escpr(data: bytes) -> tuple[EscprPacket, ...]:
    """Read one complete job after ESC/P-R entry; retain all packet boundaries.

    The spooler's REMOTE1 preamble and post-job trailer are not ESC/P-R
    packets. They are deliberately excluded. Raster bytes are skipped by
    their declared length, never searched for command-looking sequences.
    """
    start = data.find(ESCPR_ENTRY)
    if start < 0:
        raise ValueError("No ESC/P-R entry command")
    offset = start + len(ESCPR_ENTRY)
    result = []
    while offset < len(data):
        if offset + 10 > len(data):
            raise ValueError(f"Truncated ESC/P-R header at {offset}")
        if data[offset] != 0x1B:
            raise ValueError(f"Invalid ESC/P-R packet at {offset}")
        size = int.from_bytes(data[offset + 2:offset + 6], "little")
        name = data[offset + 6:offset + 10]
        if any(value < ord("a") or value > ord("z") for value in name):
            raise ValueError(f"Invalid ESC/P-R packet name at {offset}")
        end = offset + 10 + size
        if end > len(data):
            raise ValueError(f"Truncated ESC/P-R payload at {offset}")
        packet = EscprPacket(offset, end, chr(data[offset + 1]),
                             name.decode("ascii"), data[offset + 10:end])
        result.append(packet)
        offset = end
        if packet.name == "endj":
            if packet.payload:
                raise ValueError("Unexpected ESC/P-R endj payload")
            return tuple(result)
    raise ValueError("ESC/P-R job has no endj packet")
