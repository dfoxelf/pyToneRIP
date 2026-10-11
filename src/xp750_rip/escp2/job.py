# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Build restricted model-96 jobs for the Phase 2 and Phase 3C experiments."""

from __future__ import annotations

from dataclasses import dataclass, replace
from hashlib import sha256

from xp750_rip.channels import PhysicalChannel
from xp750_rip.direction import PrintDirection
from xp750_rip.escp2 import commands
from xp750_rip.escp2.channels import BLACK_INK_ADDRESS, Escp2InkAddress
from xp750_rip.escp2.model import PX700W_BASELINE, PrinterModel
from xp750_rip.escp2.packbits import encode as packbits_encode
from xp750_rip.escp2.raster import RasterPass
from xp750_rip.printers.xp750 import (
    XP750_PHASE3C,
    XP750_PHYSICAL_PATH_BY_ID,
    XP750_PHYSICAL_PATHS,
    XP750_SOURCE_CHANNEL_BY_PHYSICAL,
    Phase3CProfile,
    SourceDerivedChannel,
)
from xp750_rip.raster import NamedRasterPlane, PhysicalPathId, PhysicalRasterSet
from xp750_rip.testpatterns.patterns import (
    IdentificationPattern,
    MultipathPatternGeometry,
    PatternGeometry,
    Phase3CPatternGeometry,
    make_identification_pattern,
    make_phase2_pattern,
    make_phase3c_pattern,
)


@dataclass(frozen=True, slots=True)
class GeneratedJob:
    data: bytes
    geometry: PatternGeometry
    raster_bytes_per_row: int
    horizontal_position: int
    vertical_position: int

    @property
    def sha256(self) -> str:
        return sha256(self.data).hexdigest()


@dataclass(frozen=True, slots=True)
class GeneratedPhase3CJob:
    data: bytes
    geometry: Phase3CPatternGeometry
    passes: tuple[RasterPass, RasterPass]

    @property
    def sha256(self) -> str:
        return sha256(self.data).hexdigest()


@dataclass(frozen=True, slots=True)
class GeneratedChannelTestJob:
    data: bytes
    geometry: Phase3CPatternGeometry
    passes: tuple[RasterPass, RasterPass]
    mapping: SourceDerivedChannel

    @property
    def sha256(self) -> str:
        return sha256(self.data).hexdigest()


@dataclass(frozen=True, slots=True)
class MultipathRasterBlock:
    """One addressed physical-path raster record within a mechanical pass."""

    path: PhysicalPathId
    raster: RasterPass


@dataclass(frozen=True, slots=True)
class MultipathPass:
    """One source-derived pass containing all active path records in flush order."""

    pass_index: int
    vertical_advance: int
    blocks: tuple[MultipathRasterBlock, ...]

    def __post_init__(self) -> None:
        if not self.blocks:
            raise ValueError("a multipath pass must contain at least one active raster block")
        if any(block.raster.pass_index != self.pass_index for block in self.blocks):
            raise ValueError("every raster block must belong to the containing pass")
        if any(block.raster.vertical_advance != self.vertical_advance for block in self.blocks):
            raise ValueError("every raster block must share the containing pass advance")

    @property
    def selector_sequence(self) -> tuple[int, ...]:
        return tuple(block.raster.selector for block in self.blocks)

    def encode(self) -> bytes:
        output = bytearray(commands.relative_vertical(self.vertical_advance))
        for block in self.blocks:
            output.extend(block.raster.encode_raster_record())
        return bytes(output)

    def with_compressed_rows(self) -> MultipathPass:
        return replace(
            self,
            blocks=tuple(
                MultipathRasterBlock(block.path, block.raster.with_compressed_rows())
                for block in self.blocks
            ),
        )


@dataclass(frozen=True, slots=True)
class GeneratedMultipathJob:
    data: bytes
    rasters: PhysicalRasterSet
    passes: tuple[MultipathPass, ...]
    patterns: tuple[tuple[PhysicalPathId, MultipathPatternGeometry], ...]

    @property
    def sha256(self) -> str:
        return sha256(self.data).hexdigest()

    @property
    def active_paths(self) -> tuple[PhysicalPathId, ...]:
        return self.rasters.identities


@dataclass(frozen=True, slots=True)
class GeneratedPhysicalRasterJob:
    """A file-only job built from already-routed physical dot planes."""

    data: bytes
    rasters: PhysicalRasterSet
    passes: tuple[MultipathPass, ...]

    @property
    def sha256(self) -> str:
        return sha256(self.data).hexdigest()

    @property
    def active_paths(self) -> tuple[PhysicalPathId, ...]:
        return self.rasters.identities


def _two_plane_small_dot_row(bits: bytes) -> bytes:
    """Encode bit pattern 1 as plane 0 followed by an empty plane 1.

    Gutenprint assigns the smallest variable dot bit pattern 1
    (``dither-inks.c:350-357``) and allocates ``bitwidth * ceil(width/8)``
    bytes per line (``print-escp2.c:4449-4453``).
    """
    return bits + bytes(len(bits))


def generate_phase2_job(model: PrinterModel = PX700W_BASELINE) -> GeneratedJob:
    rows, geometry = make_phase2_pattern(model.horizontal_dpi, model.vertical_dpi)
    raw_rows = [_two_plane_small_dot_row(row) for row in rows]
    bytes_per_row = len(raw_rows[0])

    # A4 and 20 mm offsets, expressed in model-96's 180 page-management units/inch.
    page_width = round(210 / 25.4 * model.vertical_dpi)
    page_height = round(297 / 25.4 * model.vertical_dpi)
    x_position = round(20 / 25.4 * model.horizontal_dpi)
    y_position = round(20 / 25.4 * model.vertical_dpi)

    output = bytearray()
    # Exact model-96 preinitSequence, source model_96.xml:26.
    output.extend(b"\x00\x00\x00\x1b\x01@EJL 1284.4\n@EJL     \n\x1b@")
    output.extend(commands.reset())
    output.extend(commands.graphics_mode())
    output.extend(
        commands.units(
            page=model.vertical_dpi,
            vertical=model.vertical_dpi,
            horizontal=model.horizontal_dpi,
            scale=model.resolution_scale,
        )
    )
    output.extend(commands.gray_mode(1))
    output.extend(commands.microweave(0))
    output.extend(commands.unidirectional(True))
    # 0x31 is the model-96 360x180 resolution's escp2_ink_type (49).
    output.extend(commands.dot_size(0x31))
    output.extend(commands.page_length(page_height))
    output.extend(commands.page_format(0, page_height))
    output.extend(commands.paper_dimensions(page_width, page_height))
    output.extend(commands.print_method(0x11))  # artisan Plain media default.
    output.extend(commands.absolute_vertical(y_position))
    output.extend(commands.absolute_horizontal(x_position))
    output.extend(
        commands.raster_header(
            selector=model.black.selector,
            compression=1,
            bit_depth=model.bit_depth,
            bytes_per_row=bytes_per_row,
            rows=len(raw_rows),
        )
    )
    for row in raw_rows:
        output.extend(packbits_encode(row))
    output.extend(b"\r")
    output.extend(commands.form_feed())
    output.extend(commands.reset())
    return GeneratedJob(bytes(output), geometry, bytes_per_row, x_position, y_position)


def _build_phase3c_passes(
    profile: Phase3CProfile,
    pattern_rows: tuple[bytes, ...],
    address: Escp2InkAddress = BLACK_INK_ADDRESS,
) -> tuple[RasterPass, RasterPass]:
    if profile.bit_depth != 2 or profile.compression != 1:
        raise ValueError("The fixed model-96 profile requires two planes and PackBits")
    if profile.pass_count != 2 or profile.rows_per_pass != 180:
        raise ValueError("Phase 3C supports exactly two 180-row passes")
    if len(pattern_rows) != profile.pass_count * profile.rows_per_pass:
        raise ValueError("logical pattern height does not match the fixed pass schedule")
    zero_row = bytes(profile.bytes_per_plane_row)
    passes: list[RasterPass] = []
    for pass_index in range(profile.pass_count):
        start = pass_index * profile.rows_per_pass
        end = start + profile.rows_per_pass
        plane0 = pattern_rows[start:end]
        plane1 = tuple(zero_row for _ in range(profile.rows_per_pass))
        passes.append(
            RasterPass(
                pass_index=pass_index,
                vertical_advance=profile.relative_vertical_advance,
                raster_width_pixels=profile.raster_width_pixels,
                plane0_rows=plane0,
                plane1_rows=plane1,
                address=address,
            )
        )
    return passes[0], passes[1]


def _encode_fixed_model96_stream(
    *,
    profile: Phase3CProfile,
    color_configuration: int,
    job_label: str,
    encoded_passes: tuple[bytes, ...],
    blank_page: bool = False,
    direction: PrintDirection = PrintDirection.BIDIRECTIONAL,
    paper_path_type: int = 1,
    paper_path_number: int = 1,
    split_parts: bool = False,
) -> bytes | tuple[bytes, bytes]:
    """Frame fixed-profile pass bytes with the model-96 job.

    The historical stream used PP 1,1 unconditionally.  The path must follow
    the selected printer capability so the fixed 360x180 route cannot silently
    override the configured paper tray.
    """

    output = bytearray(commands.ejl_packet_mode_exit())
    output.extend(commands.reset())  # model-96 preinitSequence reset
    output.extend(commands.reset())  # generic escp2_reset_printer reset
    output.extend(commands.remote_mode_enter())
    output.extend(commands.remote_job_start(job_label))
    output.extend(commands.remote_sn(0))
    output.extend(
        commands.remote_paper_path(
            path_type=paper_path_type, path_number=paper_path_number
        )
    )
    output.extend(commands.remote_media_info(media_format=1, media_type=0, media_size=1))
    output.extend(commands.remote_mode_exit())

    output.extend(commands.graphics_mode())
    output.extend(
        commands.units(
            page=profile.vertical_dpi,
            vertical=profile.vertical_dpi,
            horizontal=profile.horizontal_dpi,
            scale=profile.unit_scale,
        )
    )
    output.extend(commands.gray_mode(color_configuration))
    output.extend(commands.microweave(0))
    bidirectional = direction is PrintDirection.BIDIRECTIONAL
    output.extend(commands.unidirectional(not bidirectional))
    output.extend(commands.dot_size(profile.dot_size))
    output.extend(
        commands.printhead_resolution(
            scale=profile.printhead_resolution_scale,
            vertical=profile.printhead_vertical_unit,
            horizontal=profile.printhead_horizontal_unit,
        )
    )
    output.extend(commands.page_length(profile.page.height_page_units))
    output.extend(
        commands.page_format(
            profile.page.format_top_vertical_units,
            profile.page.format_bottom_vertical_units,
        )
    )
    output.extend(
        commands.paper_dimensions(
            profile.page.width_page_units,
            profile.page.height_page_units,
        )
    )
    output.extend(commands.print_method(profile.print_method))
    prefix_end = len(output)
    for encoded_pass in encoded_passes:
        output.extend(encoded_pass)

    if blank_page and not split_parts:
        output.extend(commands.line_feed())
    output.extend(commands.form_feed())
    output.extend(commands.reset())
    output.extend(commands.remote_mode_enter())
    output.extend(commands.remote_load_defaults())
    output.extend(commands.remote_job_end(0))
    output.extend(commands.remote_mode_exit())
    if split_parts:
        return bytes(output[:prefix_end]), bytes(output[prefix_end:])
    return bytes(output)


def _generate_fixed_model96_job(
    *,
    profile: Phase3CProfile,
    address: Escp2InkAddress,
    color_configuration: int,
    job_label: str,
) -> tuple[bytes, Phase3CPatternGeometry, tuple[RasterPass, RasterPass]]:
    pattern_rows, geometry = make_phase3c_pattern(
        logical_width_pixels=profile.raster_width_pixels,
        logical_height_rows=profile.pass_count * profile.rows_per_pass,
        bounds=(480, 103, 975, 228),
        horizontal_dpi=profile.horizontal_dpi,
        vertical_dpi=profile.vertical_dpi,
        nozzle_separation=profile.nozzle_separation,
    )
    passes = _build_phase3c_passes(profile, pattern_rows, address)
    data = _encode_fixed_model96_stream(
        profile=profile,
        color_configuration=color_configuration,
        job_label=job_label,
        encoded_passes=tuple(raster_pass.encode() for raster_pass in passes),
    )
    return data, geometry, passes


def generate_phase3c_job(profile: Phase3CProfile = XP750_PHASE3C) -> GeneratedPhase3CJob:
    """Generate the fixed Phase 3C candidate without reading the golden PRN."""

    if profile.selector != 0:
        raise ValueError("Phase 3C remains restricted to selector 0")
    data, geometry, passes = _generate_fixed_model96_job(
        profile=profile,
        address=Escp2InkAddress.from_selector(profile.selector),
        color_configuration=1,
        job_label="XP750RIP",
    )
    return GeneratedPhase3CJob(data, geometry, passes)


def generate_channel_test_job(
    channel: PhysicalChannel,
    profile: Phase3CProfile = XP750_PHASE3C,
) -> GeneratedChannelTestJob:
    """Generate one source-derived Phase 4 channel-isolation candidate."""

    mapping = XP750_SOURCE_CHANNEL_BY_PHYSICAL[channel]
    data, geometry, passes = _generate_fixed_model96_job(
        profile=profile,
        address=mapping.address,
        color_configuration=2,
        job_label="XP750RIP",
    )
    return GeneratedChannelTestJob(data, geometry, passes, mapping)


_XP750_MULTIPATH_PATTERN_LAYOUT = (
    (PhysicalPathId("P0"), IdentificationPattern.VERTICAL_LINE, (400, 150, 495, 209)),
    (PhysicalPathId("P1"), IdentificationPattern.HORIZONTAL_LINE, (720, 150, 815, 209)),
    (PhysicalPathId("P2"), IdentificationPattern.BACKSLASH, (1040, 150, 1135, 209)),
    (PhysicalPathId("P3"), IdentificationPattern.SLASH, (1360, 150, 1455, 209)),
    (PhysicalPathId("P4"), IdentificationPattern.RECTANGLE, (1680, 150, 1775, 209)),
    (PhysicalPathId("P5"), IdentificationPattern.CROSS, (2000, 150, 2095, 209)),
)


def _make_xp750_multipath_rasters(
    active_paths: tuple[PhysicalPathId, ...],
    profile: Phase3CProfile,
) -> tuple[PhysicalRasterSet, tuple[tuple[PhysicalPathId, MultipathPatternGeometry], ...]]:
    layout = {path: (kind, bounds) for path, kind, bounds in _XP750_MULTIPATH_PATTERN_LAYOUT}
    height_rows = profile.pass_count * profile.rows_per_pass
    zero_row = bytes(profile.bytes_per_plane_row)
    zero_plane = tuple(zero_row for _ in range(height_rows))
    planes: list[NamedRasterPlane] = []
    patterns: list[tuple[PhysicalPathId, MultipathPatternGeometry]] = []
    for path in active_paths:
        try:
            kind, bounds = layout[path]
        except KeyError as error:
            raise ValueError(f"no Phase 5 identification pattern is defined for {path}") from error
        pattern_rows, geometry = make_identification_pattern(
            kind=kind,
            logical_width_pixels=profile.raster_width_pixels,
            logical_height_rows=height_rows,
            bounds=bounds,
            horizontal_dpi=profile.horizontal_dpi,
            vertical_dpi=profile.vertical_dpi,
            nozzle_separation=profile.nozzle_separation,
        )
        planes.append(
            NamedRasterPlane(
                identity=path,
                width_pixels=profile.raster_width_pixels,
                bit_plane_rows=(pattern_rows, zero_plane),
            )
        )
        patterns.append((path, geometry))
    return PhysicalRasterSet(tuple(planes)), tuple(patterns)


def schedule_xp750_multipath_passes(
    rasters: PhysicalRasterSet,
    profile: Phase3CProfile,
) -> tuple[MultipathPass, ...]:
    """Apply the source-derived model-96 flush order for the fixed 360x180 mode.

    Gutenprint scales all model-96 raw head offsets to zero at 180 vertical dpi.
    Refuse to reuse this reduced schedule if that invariant changes.
    """

    expected_height = profile.pass_count * profile.rows_per_pass
    if (
        rasters.width_pixels != profile.raster_width_pixels
        or rasters.height_rows != expected_height
        or rasters.bit_depth != profile.bit_depth
    ):
        raise ValueError("physical raster geometry does not match the fixed XP-750 profile")
    unknown = set(rasters.identities) - set(XP750_PHYSICAL_PATH_BY_ID)
    if unknown:
        names = ", ".join(sorted(path.value for path in unknown))
        raise ValueError(f"unknown XP-750 physical paths: {names}")

    descriptors = tuple(XP750_PHYSICAL_PATH_BY_ID[path] for path in rasters.identities)
    effective_offsets = tuple(
        descriptor.effective_head_offset(
            vertical_dpi=profile.vertical_dpi,
            base_separation_dpi=profile.base_separation_dpi,
        )
        for descriptor in descriptors
    )
    if any(effective_offsets):
        raise ValueError(
            "the fixed Phase 5 scheduler is valid only when every effective head offset is zero"
        )

    ordered_planes = tuple(
        sorted(
            rasters.planes,
            key=lambda plane: XP750_PHYSICAL_PATH_BY_ID[
                plane.identity
            ].gutenprint_allocation_index,
        )
    )
    passes: list[MultipathPass] = []
    for pass_index in range(profile.pass_count):
        start = pass_index * profile.rows_per_pass
        end = start + profile.rows_per_pass
        blocks: list[MultipathRasterBlock] = []
        for plane in ordered_planes:
            sliced_bit_planes = tuple(bit_plane[start:end] for bit_plane in plane.bit_plane_rows)
            if not any(any(row) for bit_plane in sliced_bit_planes for row in bit_plane):
                continue
            descriptor = XP750_PHYSICAL_PATH_BY_ID[plane.identity]
            raster_pass = RasterPass(
                pass_index=pass_index,
                vertical_advance=profile.relative_vertical_advance,
                raster_width_pixels=profile.raster_width_pixels,
                plane0_rows=sliced_bit_planes[0],
                plane1_rows=sliced_bit_planes[1],
                address=descriptor.address,
            )
            blocks.append(MultipathRasterBlock(path=plane.identity, raster=raster_pass))
        if blocks:
            passes.append(
                MultipathPass(
                    pass_index=pass_index,
                    vertical_advance=profile.relative_vertical_advance,
                    blocks=tuple(blocks),
                )
            )
    return tuple(passes)


def generate_xp750_physical_raster_job(
    rasters: PhysicalRasterSet,
    *,
    job_label: str = "XP750RIP",
    profile: Phase3CProfile = XP750_PHASE3C,
) -> GeneratedPhysicalRasterJob:
    """Frame routed dot planes with the unchanged verified model-96 backend."""

    passes = schedule_xp750_multipath_passes(rasters, profile)
    if not passes:
        raise ValueError("physical raster job contains no active dot data")
    data = _encode_fixed_model96_stream(
        profile=profile,
        color_configuration=2,
        job_label=job_label,
        encoded_passes=tuple(item.encode() for item in passes),
    )
    return GeneratedPhysicalRasterJob(data=data, rasters=rasters, passes=passes)


def generate_xp750_multipath_job(
    active_paths: tuple[PhysicalPathId, ...],
    profile: Phase3CProfile = XP750_PHASE3C,
) -> GeneratedMultipathJob:
    """Generate one independently patterned, file-only XP-750 multipath job."""

    if not 2 <= len(active_paths) <= len(XP750_PHYSICAL_PATHS):
        raise ValueError("Phase 5 requires between two and six active XP-750 paths")
    if len(set(active_paths)) != len(active_paths):
        raise ValueError("active physical paths must be unique")
    if any(path not in XP750_PHYSICAL_PATH_BY_ID for path in active_paths):
        raise ValueError("active physical path set contains an unknown XP-750 path")

    rasters, patterns = _make_xp750_multipath_rasters(active_paths, profile)
    passes = schedule_xp750_multipath_passes(rasters, profile)
    if len(passes) != profile.pass_count:
        raise ValueError("Phase 5 identification patterns must activate both fixed passes")
    expected_paths = set(active_paths)
    if any({block.path for block in item.blocks} != expected_paths for item in passes):
        raise ValueError("every Phase 5 path must remain active in both fixed passes")
    data = _encode_fixed_model96_stream(
        profile=profile,
        color_configuration=2,
        job_label="XP750RIP",
        encoded_passes=tuple(item.encode() for item in passes),
    )
    return GeneratedMultipathJob(data=data, rasters=rasters, passes=passes, patterns=patterns)


def generate_xp750_multipath_test_job(
    path_count: int,
    profile: Phase3CProfile = XP750_PHASE3C,
) -> GeneratedMultipathJob:
    """Generate the Phase 5 incremental P0..P(n-1) candidate."""

    if not 2 <= path_count <= len(XP750_PHYSICAL_PATHS):
        raise ValueError("XP-750 Phase 5 path count must be in the range 2..6")
    active_paths = tuple(item.identity for item in XP750_PHYSICAL_PATHS[:path_count])
    return generate_xp750_multipath_job(active_paths, profile)
