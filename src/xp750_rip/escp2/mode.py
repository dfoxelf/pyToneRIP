# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Source-derived Epson ESC/P2 mode mechanics for the model-96 backend.

This module is deliberately printer-backend data.  Generic QTR, routing and
raster objects do not depend on the XP-750 having six physical paths.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class EpsonEscp2ModeParameters:
    """The model fields consumed by Gutenprint's ESC/P2/softweave path."""

    identity: str
    physical_horizontal_dpi: int
    physical_vertical_dpi: int
    printed_horizontal_dpi: int
    printed_vertical_dpi: int
    base_resolution_dpi: int
    vertical_passes: int
    density: float
    escp2_ink_type: int
    bit_depth: int
    drop_sizes: tuple[float, ...]
    ordered_matrix_id: str
    nozzle_count: int = 180
    raw_nozzle_separation: int = 2
    base_separation_dpi: int = 360
    resolution_scale: int = 14400
    unit_scale: int = 5760
    vertical_oversample: int = 1
    separation_rows: int = 1
    weave_strategy: str = "zigzag"
    bidirectional: bool = True
    printer_weave_mode: int | None = None
    command_vertical_units: int | None = None
    command_horizontal_units: int | None = None

    @property
    def physical_xdpi(self) -> int:
        return min(self.base_resolution_dpi, self.physical_horizontal_dpi)

    @property
    def horizontal_passes(self) -> int:
        return self.printed_horizontal_dpi // self.physical_xdpi

    @property
    def effective_nozzle_separation(self) -> int:
        # Older large-format heads can expose one base-resolution row whose
        # scaled separation would truncate to zero at draft resolutions. A
        # physical nozzle separation is always at least one raster row.
        return max(
            1,
            self.raw_nozzle_separation
            * self.physical_vertical_dpi
            // self.base_separation_dpi,
        )

    @property
    def oversample(self) -> int:
        return self.horizontal_passes * self.vertical_passes * self.vertical_oversample

    @property
    def horizontal_units(self) -> int:
        return self.command_horizontal_units or self.physical_horizontal_dpi

    @property
    def vertical_units(self) -> int:
        if self.command_vertical_units is not None:
            return self.command_vertical_units
        # print-escp2.c:3915-3930, adjusted_vertical_resolution().
        if (
            self.physical_vertical_dpi < self.base_separation_dpi * 2
            and self.physical_horizontal_dpi >= self.base_separation_dpi * 2
        ):
            return self.base_separation_dpi * 2
        return self.physical_vertical_dpi

    @property
    def page_management_units(self) -> int:
        return self.vertical_units

    @property
    def printhead_vertical_unit(self) -> int:
        if self.printer_weave_mode is not None:
            return self.resolution_scale // self.physical_vertical_dpi
        return (
            self.resolution_scale
            * self.raw_nozzle_separation
            // self.base_separation_dpi
        )

    @property
    def printhead_horizontal_unit(self) -> int:
        return self.resolution_scale // self.physical_xdpi

    def effective_head_offset(self, raw_offset: int) -> int:
        return raw_offset * self.physical_vertical_dpi // self.base_separation_dpi


def _mode(
    identity: str,
    physical: tuple[int, int],
    *,
    printed: tuple[int, int] | None = None,
    base: int,
    vertical_passes: int = 1,
    density: float,
    ink_type: int,
    drops: tuple[float, ...],
) -> EpsonEscp2ModeParameters:
    printed = printed or physical
    aspect = printed[0] // printed[1]
    if aspect not in (1, 2, 4):
        raise ValueError(f"model96 Ordered aspect ratio is unsupported: {aspect}")
    return EpsonEscp2ModeParameters(
        identity=identity,
        physical_horizontal_dpi=physical[0],
        physical_vertical_dpi=physical[1],
        printed_horizontal_dpi=printed[0],
        printed_vertical_dpi=printed[1],
        base_resolution_dpi=base,
        vertical_passes=vertical_passes,
        density=density,
        escp2_ink_type=ink_type,
        bit_depth=2,
        drop_sizes=drops,
        ordered_matrix_id=f"matrix-{aspect}x1",
    )


# FACT: complete resolution list in model_96.xml.  The order is source order.
XP750_MODEL96_MODES = (
    _mode("360x180sw", (360, 180), base=360, density=4.0, ink_type=49,
          drops=(0.36, 0.60, 1.0)),
    _mode("360sw", (360, 360), base=360, density=4.0, ink_type=49,
          drops=(0.36, 0.60, 1.0)),
    _mode("720x360sw", (720, 360), base=720, vertical_passes=4,
          density=2.45, ink_type=33, drops=(0.16, 0.58, 1.0)),
    _mode("720sw", (720, 720), base=720, density=1.224, ink_type=33,
          drops=(0.13, 0.58, 1.0)),
    _mode("1440x720sw", (1440, 720), base=720, density=0.612, ink_type=33,
          drops=(0.13, 0.58, 1.0)),
    _mode("1440x1440ov", (2880, 1440), printed=(1440, 1440), base=720,
          density=1.177, ink_type=37, drops=(0.50, 0.60, 1.0)),
    _mode("2880x1440sw", (2880, 1440), base=720, density=0.586, ink_type=37,
          drops=(0.50, 0.60, 1.0)),
    _mode("5760x1440sw", (5760, 1440), base=720, density=0.4398, ink_type=37,
          drops=(0.50, 0.60, 1.0)),
    _mode("2880x2880sw", (2880, 2880), base=720, density=0.4398, ink_type=37,
          drops=(0.50, 0.60, 1.0)),
    _mode("5760x2880sw", (5760, 2880), base=720, density=0.2199, ink_type=37,
          drops=(0.50, 0.60, 1.0)),
)

XP750_MODEL96_MODE_BY_ID = {item.identity: item for item in XP750_MODEL96_MODES}
