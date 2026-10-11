# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Typed printer data for the user-verified PX700W/model-96 baseline."""

from dataclasses import dataclass
from enum import StrEnum


class Confidence(StrEnum):
    """Project evidence classification for individual model fields."""

    VERIFIED_BASELINE = "VERIFIED_BASELINE"
    SOURCE_DERIVED = "SOURCE_DERIVED"
    EXPERIMENTALLY_CONFIRMED = "EXPERIMENTALLY_CONFIRMED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class Escp2Channel:
    name: str
    selector: int
    subchannel: int
    expected_ink_path: str
    confidence: Confidence


@dataclass(frozen=True, slots=True)
class PrinterModel:
    target_name: str
    reference_name: str
    gutenprint_driver: str
    gutenprint_model: int
    gutenprint_parameters: str
    command_set: int
    physical_channels: int
    nozzles: int
    nozzle_separation: int
    base_separation_dpi: int
    resolution_scale: int
    horizontal_dpi: int
    vertical_dpi: int
    bit_depth: int
    compression: str
    black: Escp2Channel


PX700W_BASELINE = PrinterModel(
    target_name="Epson XP-750",
    reference_name="Epson Stylus Photo PX700W",
    gutenprint_driver="escp2-px700fw",
    gutenprint_model=96,
    gutenprint_parameters="claria_params",
    command_set=2000,
    physical_channels=6,
    nozzles=180,
    nozzle_separation=2,
    base_separation_dpi=360,
    resolution_scale=14400,
    horizontal_dpi=360,
    vertical_dpi=180,
    bit_depth=2,
    compression="TIFF PackBits",
    black=Escp2Channel(
        name="Black",
        selector=0,
        subchannel=0,
        expected_ink_path="reported Black cartridge/path",
        confidence=Confidence.VERIFIED_BASELINE,
    ),
)
