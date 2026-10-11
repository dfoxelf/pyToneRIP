# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""XP-750 target using the user-verified PX700W Gutenprint baseline."""

from dataclasses import dataclass
from enum import StrEnum

from xp750_rip.channels import PhysicalChannel
from xp750_rip.escp2.channels import Escp2InkAddress
from xp750_rip.escp2.model import PX700W_BASELINE
from xp750_rip.raster import PhysicalPathId

XP750 = PX700W_BASELINE


@dataclass(frozen=True, slots=True)
class LetterPageGeometry:
    """Verified Letter values serialized by model 96 in the golden stream."""

    name: str = "Letter"
    width_inches: float = 8.5
    height_inches: float = 11.0
    width_page_units: int = 1530
    height_page_units: int = 1980
    format_top_vertical_units: int = -180
    format_bottom_vertical_units: int = 2140


@dataclass(frozen=True, slots=True)
class Phase3CProfile:
    """Single fixed model-96 profile authorized for the Phase 3C experiment."""

    target_device: str = "Epson XP-750"
    reference_model: str = "Epson Stylus Photo PX700W"
    gutenprint_driver: str = "escp2-px700fw"
    gutenprint_model: int = 96
    gutenprint_parameters: str = "claria_params"
    resolution_name: str = "360x180sw"
    horizontal_dpi: int = 360
    vertical_dpi: int = 180
    media: str = "Plain"
    input_slot: str = "LowerFront"
    color_mode: str = "Gray"
    ink_type: str = "PhotoCMYK"
    color_correction: str = "None"
    quality: str = "Standard"
    unit_scale: int = 5760
    printhead_resolution_scale: int = 14400
    printhead_vertical_unit: int = 80
    printhead_horizontal_unit: int = 40
    dot_size: int = 0x31
    print_method: int = 0x10
    selector: int = 0x00
    bit_depth: int = 2
    compression: int = 1
    raster_width_pixels: int = 2976
    rows_per_pass: int = 180
    pass_count: int = 2
    relative_vertical_advance: int = 180
    nozzles: int = 180
    nozzle_separation: int = 2
    base_separation_dpi: int = 360
    page: LetterPageGeometry = LetterPageGeometry()

    @property
    def bytes_per_plane_row(self) -> int:
        return self.raster_width_pixels // 8

    @property
    def bytes_per_raster_row(self) -> int:
        return self.bytes_per_plane_row * self.bit_depth


XP750_PHASE3C = Phase3CProfile()


class MappingConfidence(StrEnum):
    """Evidence levels kept separate from physical-test status."""

    SOURCE_CONFIRMED = "SOURCE_CONFIRMED"
    INFERENCE = "INFERENCE"


class PhysicalTestStatus(StrEnum):
    """Allowed states for isolated physical-channel verification."""

    NOT_TESTED = "NOT_TESTED"
    CONFIRMED = "CONFIRMED"
    CONTRADICTED = "CONTRADICTED"


@dataclass(frozen=True, slots=True)
class SourceDerivedChannel:
    """One model-96 PhotoCMYK physical subchannel and its ESC/P2 address."""

    physical_channel: PhysicalChannel
    logical_channel: str
    logical_channel_index: int
    subchannel_name: str
    subchannel_index: int
    relative_shade: float
    head_offset: int
    allocation_index: int
    model_channel_name_index: int
    address: Escp2InkAddress
    address_confidence: MappingConfidence = MappingConfidence.SOURCE_CONFIRMED
    expected_path_confidence: MappingConfidence = MappingConfidence.INFERENCE
    physical_test_status: PhysicalTestStatus = PhysicalTestStatus.NOT_TESTED


XP750_SOURCE_CHANNELS = (
    SourceDerivedChannel(
        PhysicalChannel.K,
        "K",
        0,
        "Black",
        0,
        1.0,
        1,
        0,
        3,
        Escp2InkAddress(0, 0),
    ),
    SourceDerivedChannel(
        PhysicalChannel.C,
        "C",
        1,
        "Cyan",
        0,
        1.0,
        1,
        1,
        0,
        Escp2InkAddress(2, 0),
    ),
    SourceDerivedChannel(
        PhysicalChannel.M,
        "M",
        2,
        "Magenta",
        0,
        1.0,
        1,
        3,
        4,
        Escp2InkAddress(1, 0),
    ),
    SourceDerivedChannel(
        PhysicalChannel.Y,
        "Y",
        3,
        "Yellow",
        0,
        1.0,
        0,
        5,
        1,
        Escp2InkAddress(4, 0),
    ),
    SourceDerivedChannel(
        PhysicalChannel.LC,
        "C",
        1,
        "LightCyan",
        1,
        0.35,
        0,
        2,
        2,
        Escp2InkAddress(2, 1),
    ),
    SourceDerivedChannel(
        PhysicalChannel.LM,
        "M",
        2,
        "LightMagenta",
        1,
        0.33,
        0,
        4,
        5,
        Escp2InkAddress(1, 1),
    ),
)

XP750_SOURCE_CHANNEL_BY_PHYSICAL = {
    item.physical_channel: item for item in XP750_SOURCE_CHANNELS
}


@dataclass(frozen=True, slots=True)
class XP750PhysicalPathDescriptor:
    """Model-96 backend data for one physically confirmed XP-750 path."""

    identity: PhysicalPathId
    factory_role: PhysicalChannel
    address: Escp2InkAddress
    gutenprint_head_offset: int
    gutenprint_allocation_index: int

    def effective_head_offset(self, *, vertical_dpi: int, base_separation_dpi: int) -> int:
        """Apply Gutenprint's integer model-head-offset scaling."""

        if vertical_dpi <= 0 or base_separation_dpi <= 0:
            raise ValueError("head-offset resolution values must be positive")
        return self.gutenprint_head_offset * vertical_dpi // base_separation_dpi


# P0..P5 are physical-path identities. K/C/M/Y/LC/LM remain factory metadata.
XP750_PHYSICAL_PATHS = tuple(
    XP750PhysicalPathDescriptor(
        identity=PhysicalPathId(f"P{index}"),
        factory_role=mapping.physical_channel,
        address=mapping.address,
        gutenprint_head_offset=mapping.head_offset,
        gutenprint_allocation_index=mapping.allocation_index,
    )
    for index, mapping in enumerate(XP750_SOURCE_CHANNELS)
)

XP750_PHYSICAL_PATH_BY_ID = {item.identity: item for item in XP750_PHYSICAL_PATHS}

# Physical test order: established K control, three dark chromatic paths,
# then each light path immediately after its dark-family comparison is known.
XP750_RECOMMENDED_TEST_ORDER = (
    PhysicalChannel.K,
    PhysicalChannel.Y,
    PhysicalChannel.C,
    PhysicalChannel.LC,
    PhysicalChannel.M,
    PhysicalChannel.LM,
)
