# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Small Python port of the Gutenprint softweave mechanics used by model 96.

The mapping follows ``print-weave.c`` (raw weave, cooked leading/trailing
pass maps and row assignment).  Pixel packing follows ``bit-ops.c``
``stp_fold``/``stp_unpack``/``stp_split`` semantics for two-bit samples.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from math import gcd
from time import perf_counter
from typing import TYPE_CHECKING

import numpy as np

from xp750_rip.escp2 import commands
from xp750_rip.escp2.channels import Escp2InkAddress
from xp750_rip.escp2.mode import EpsonEscp2ModeParameters
from xp750_rip.escp2.packbits import encode as packbits_encode
from xp750_rip.printers.capabilities import PhysicalPathDefinition
from xp750_rip.printers.xp750 import XP750_PHYSICAL_PATH_BY_ID
from xp750_rip.raster import (
    NamedRasterPlane,
    PhysicalPathId,
    PhysicalRasterSet,
    StreamingPhysicalRasterSet,
)

if TYPE_CHECKING:
    from xp750_rip.performance import PerformanceTrace


@dataclass(slots=True)
class WeaveStageTimings:
    """Aggregate timings for interleaved source-compatible weave operations."""

    horizontal_unpack: float = 0.0
    vertical_split: float = 0.0
    two_bit_fold: float = 0.0
    cython_unpack_split_fold: float = 0.0
    cython_assignment: float = 0.0
    packbits: float = 0.0
    escp2_raster_construction: float = 0.0


@dataclass(frozen=True, slots=True)
class WeaveRow:
    source_row: int
    pass_index: int
    subpass: int
    nozzle: int
    logical_pass_start: int
    physical_pass_start: int
    physical_pass_end: int
    missing_start_rows: int


@dataclass(frozen=True, slots=True)
class PackedRasterPass:
    pass_index: int
    vertical_advance: int
    raster_width_pixels: int
    decoded_rows: tuple[bytes, ...]
    address: Escp2InkAddress
    source_rows: tuple[int | None, ...]
    nozzle_rows: tuple[int, ...]
    horizontal_position: int = 0
    bit_depth: int = 2
    compression: int = 1
    encoded_rows: tuple[bytes, ...] | None = field(
        default=None, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if not self.decoded_rows:
            raise ValueError("softweave raster block cannot be empty")
        expected = self.bytes_per_raster_row
        if any(len(row) != expected for row in self.decoded_rows):
            raise ValueError("softweave packed row width mismatch")
        if len(self.source_rows) != len(self.decoded_rows):
            raise ValueError("softweave source-row trace length mismatch")
        if self.encoded_rows is not None and len(self.encoded_rows) != len(
            self.decoded_rows
        ):
            raise ValueError("softweave encoded-row cache length mismatch")

    @property
    def selector(self) -> int:
        return self.address.selector

    @property
    def row_count(self) -> int:
        return len(self.decoded_rows)

    @property
    def bytes_per_raster_row(self) -> int:
        return self.bit_depth * ((self.raster_width_pixels + 7) // 8)

    @property
    def compressed_rows(self) -> tuple[bytes, ...]:
        if self.encoded_rows is not None:
            return self.encoded_rows
        return tuple(packbits_encode(row) for row in self.decoded_rows)

    def with_compressed_rows(
        self, timings: WeaveStageTimings | None = None
    ) -> PackedRasterPass:
        if self.encoded_rows is not None:
            return self
        started = perf_counter()
        encoded = tuple(packbits_encode(row) for row in self.decoded_rows)
        if timings is not None:
            timings.packbits += perf_counter() - started
        return replace(self, encoded_rows=encoded)

    def encode_raster_record(self, timings: WeaveStageTimings | None = None) -> bytes:
        started = perf_counter()
        output = bytearray(
            commands.addressed_raster_header(
                address=self.address,
                compression=self.compression,
                bit_depth=self.bit_depth,
                bytes_per_row=self.bytes_per_raster_row,
                rows=self.row_count,
            )
        )
        if timings is not None:
            timings.escp2_raster_construction += perf_counter() - started
        started = perf_counter()
        compressed_rows = self.compressed_rows
        if timings is not None:
            timings.packbits += perf_counter() - started
        started = perf_counter()
        for row in compressed_rows:
            output.extend(row)
        output.extend(commands.carriage_return())
        if timings is not None:
            timings.escp2_raster_construction += perf_counter() - started
        return bytes(output)


@dataclass(frozen=True, slots=True)
class WeaveRasterBlock:
    path: PhysicalPathId
    raster: PackedRasterPass


@dataclass(frozen=True, slots=True)
class WeavePass:
    pass_index: int
    vertical_advance: int
    logical_pass_start: int
    physical_pass_start: int
    physical_pass_end: int
    subpass: int
    blocks: tuple[WeaveRasterBlock, ...]

    @property
    def selector_sequence(self) -> tuple[int, ...]:
        return tuple(item.raster.selector for item in self.blocks)

    def encode(self, timings: WeaveStageTimings | None = None) -> bytes:
        started = perf_counter()
        output = bytearray(commands.relative_vertical(self.vertical_advance))
        if timings is not None:
            timings.escp2_raster_construction += perf_counter() - started
        for block in self.blocks:
            started = perf_counter()
            if block.raster.horizontal_position:
                output.extend(commands.absolute_horizontal(block.raster.horizontal_position))
            if timings is not None:
                timings.escp2_raster_construction += perf_counter() - started
            output.extend(block.raster.encode_raster_record(timings))
        return bytes(output)

    def with_compressed_rows(
        self, timings: WeaveStageTimings | None = None
    ) -> WeavePass:
        return replace(
            self,
            blocks=tuple(
                WeaveRasterBlock(block.path, block.raster.with_compressed_rows(timings))
                for block in self.blocks
            ),
        )


@dataclass(frozen=True, slots=True)
class WeaveSchedule:
    passes: tuple[WeavePass, ...]
    rows: tuple[tuple[PhysicalPathId, WeaveRow], ...]
    first_line: int
    page_height_rows: int


@dataclass(slots=True)
class _RawWeave:
    separation: int
    jets: int
    oversampling: int

    @property
    def advance_basis(self) -> int:
        return max(1, self.jets // self.oversampling)

    @property
    def subblocks_per_passblock(self) -> int:
        return gcd(self.separation, self.advance_basis)

    @property
    def passes_per_subblock(self) -> int:
        return self.separation // self.subblocks_per_passblock

    def pass_parameters(self, pass_index: int) -> tuple[int, int]:
        band = pass_index // (self.separation * self.oversampling)
        pass_in_band = pass_index % (self.separation * self.oversampling)
        subpass_block = (
            pass_index % self.separation
            * self.subblocks_per_passblock
            // self.separation
        )
        # model96 uses STP_WEAVE_ZIGZAG (the default in escp2_print_page).
        if subpass_block * 2 < self.subblocks_per_passblock:
            subpass_offset = 2 * subpass_block
        else:
            subpass_offset = 2 * (self.subblocks_per_passblock - subpass_block) - 1
        start = (
            self.separation * self.jets * band
            + self.advance_basis * pass_in_band
            + subpass_offset
        )
        return start, pass_in_band // self.separation

    def row_parameters(self, row: int, subpass: int) -> tuple[int, int, int]:
        subblock_offset = row % self.subblocks_per_passblock
        if subblock_offset % 2 == 0:
            subpass_block = subblock_offset // 2
        else:
            subpass_block = self.subblocks_per_passblock - (subblock_offset + 1) // 2

        band = row // (self.separation * self.jets)
        base_row = row - subblock_offset - band * self.separation * self.jets
        pass_in_band = base_row // self.advance_basis
        offset = base_row % self.advance_basis
        pass_div_separation = pass_in_band // self.separation
        pass_mod_separation = pass_in_band % self.separation
        off_mod_separation = offset % self.separation

        while (
            off_mod_separation != 0
            or pass_div_separation != subpass
            or pass_mod_separation // self.passes_per_subblock != subpass_block
        ):
            offset += self.advance_basis
            pass_in_band -= 1
            if pass_in_band >= 0:
                pass_mod_separation -= 1
                if pass_mod_separation < 0:
                    pass_mod_separation += self.separation
                    pass_div_separation -= 1
                if self.advance_basis < self.separation:
                    off_mod_separation += self.advance_basis
                    if off_mod_separation >= self.separation:
                        off_mod_separation -= self.separation
                elif self.advance_basis > self.separation:
                    off_mod_separation = offset % self.separation
            else:
                rounded_jets = (self.advance_basis * self.oversampling) % self.jets
                band -= 1
                pass_in_band += self.separation * self.oversampling
                offset += self.separation * (self.jets - rounded_jets)
                pass_div_separation = pass_in_band // self.separation
                pass_mod_separation = pass_in_band % self.separation
                off_mod_separation = offset % self.separation

        pass_index = band * self.oversampling * self.separation + pass_in_band
        jet = (offset // self.separation) % self.jets
        return pass_index, jet, row - jet * self.separation


def _sort_stable(map_values: list[int], starts: list[int]) -> None:
    pairs = sorted(zip(starts, range(len(starts)), map_values, strict=True))
    starts[:] = [item[0] for item in pairs]
    map_values[:] = [item[2] for item in pairs]


def _make_pass_map(
    raw: _RawWeave,
    *,
    first_pass_number: int,
    first_pass_to_map: int,
    first_pass_after_map: int,
    first_pass_to_stagger: int,
    first_pass_after_stagger: int,
    first_row_of_maximal_pass: int,
    separations_to_distribute: int,
) -> tuple[dict[int, int], dict[int, int]]:
    count = first_pass_after_map - first_pass_to_map
    pass_map: list[int] = []
    starts: list[int] = []
    for pass_index in range(first_pass_to_map, first_pass_after_map):
        start, _ = raw.pass_parameters(pass_index)
        if first_row_of_maximal_pass >= 0:
            start = first_row_of_maximal_pass - start + raw.separation * raw.jets
        else:
            start -= raw.separation * raw.jets
        while start < 0:
            start += raw.separation
        pass_map.append(pass_index)
        starts.append(start)
    _sort_stable(pass_map, starts)

    separations_to_distribute += 1
    stagger_count = first_pass_after_stagger - first_pass_to_stagger
    offset = first_pass_to_stagger - first_pass_to_map
    for index in range(stagger_count):
        target = index + offset
        if starts[target] // raw.separation < index % separations_to_distribute:
            starts[target] = (
                starts[target] % raw.separation
                + raw.separation * (index % separations_to_distribute)
            )
    if first_row_of_maximal_pass >= 0:
        starts[:] = [first_row_of_maximal_pass - value for value in starts]
    _sort_stable(pass_map, starts)

    staggers: list[int] = []
    for mapped, desired_start in zip(pass_map, starts, strict=True):
        raw_start, _ = raw.pass_parameters(mapped)
        raw_start -= raw.separation * raw.jets
        staggers.append((desired_start - raw_start) // raw.separation)

    result_map: dict[int, int] = {}
    result_stagger: dict[int, int] = {}
    for index in range(count):
        key = pass_map[index]
        result_map[key] = index + first_pass_to_map - first_pass_number
        result_stagger[key] = staggers[index]
    return result_map, result_stagger


@dataclass(slots=True)
class _CookedWeave:
    raw: _RawWeave
    first_row: int
    last_row: int
    page_height: int
    first_premapped: int = 0
    first_normal: int = 0
    first_postmapped: int = 0
    first_unused: int = 0
    premap: dict[int, int] | None = None
    prestagger: dict[int, int] | None = None
    postmap: dict[int, int] | None = None
    poststagger: dict[int, int] | None = None

    def __post_init__(self) -> None:
        if self.page_height <= self.last_row:
            self.page_height = self.last_row + 1
        pass_index = -1
        while True:
            pass_index += 1
            start, _ = self.raw.pass_parameters(pass_index)
            if start - self.raw.separation >= self.first_row:
                break
        self.first_premapped = pass_index
        while (
            start < self.raw.separation * self.raw.jets
            and start - self.raw.separation < self.page_height
            and start <= self.last_row + self.raw.separation * self.raw.jets
        ):
            pass_index += 1
            start, _ = self.raw.pass_parameters(pass_index)
        self.first_normal = pass_index
        while (
            start - self.raw.separation < self.page_height
            and start <= self.last_row + self.raw.separation * self.raw.jets
        ):
            pass_index += 1
            start, _ = self.raw.pass_parameters(pass_index)
        self.first_postmapped = pass_index
        while start <= self.last_row + self.raw.separation * self.raw.jets:
            pass_index += 1
            start, _ = self.raw.pass_parameters(pass_index)
        self.first_unused = pass_index

        if self.first_normal > self.first_premapped:
            distribute = self.first_row // self.raw.separation
            spread = (distribute + 1) * self.raw.separation
            normal_count = (spread + self.raw.advance_basis - 1) // self.raw.advance_basis
            self.first_normal += normal_count
            self.premap, self.prestagger = _make_pass_map(
                self.raw,
                first_pass_number=self.first_premapped,
                first_pass_to_map=self.first_premapped,
                first_pass_after_map=self.first_normal,
                first_pass_to_stagger=self.first_premapped,
                first_pass_after_stagger=self.first_normal - normal_count,
                first_row_of_maximal_pass=-1,
                separations_to_distribute=distribute,
            )
        if self.first_unused >= self.first_postmapped:
            distribute = (self.page_height - self.last_row - 1) // self.raw.separation
            spread = (distribute + 1) * self.raw.separation
            normal_count = (spread + self.raw.advance_basis) // self.raw.advance_basis
            self.first_postmapped -= normal_count
            self.postmap, self.poststagger = _make_pass_map(
                self.raw,
                first_pass_number=self.first_premapped,
                first_pass_to_map=self.first_postmapped,
                first_pass_after_map=self.first_unused,
                first_pass_to_stagger=self.first_postmapped + normal_count,
                first_pass_after_stagger=self.first_unused,
                first_row_of_maximal_pass=(
                    self.page_height - 1 - self.raw.separation * (self.raw.jets - 1)
                ),
                separations_to_distribute=distribute,
            )

    def row_parameters(self, row: int, subpass: int) -> WeaveRow:
        raw_pass, jet, start = self.raw.row_parameters(
            row + self.raw.separation * self.raw.jets, subpass
        )
        start -= self.raw.separation * self.raw.jets
        stagger = 0
        if raw_pass < self.first_normal:
            assert self.premap is not None and self.prestagger is not None
            pass_index = self.premap[raw_pass]
            stagger = self.prestagger[raw_pass]
        elif raw_pass >= self.first_postmapped:
            assert self.postmap is not None and self.poststagger is not None
            pass_index = self.postmap[raw_pass]
            stagger = self.poststagger[raw_pass]
        else:
            pass_index = raw_pass - self.first_premapped

        start += stagger * self.raw.separation
        jet -= stagger
        missing = 0
        jets_used = self.raw.jets
        if stagger < 0:
            missing = -stagger
            jets_used += stagger
        extra = self.first_row - (start + self.raw.separation * missing)
        if extra > 0:
            extra = (extra + self.raw.separation - 1) // self.raw.separation
            jets_used -= extra
            missing += extra
        extra = (
            start
            + self.raw.separation * (missing + jets_used - 1)
            - self.last_row
        )
        if extra > 0:
            jets_used -= (extra + self.raw.separation - 1) // self.raw.separation
        physical_start = start + self.raw.separation * missing
        physical_end = physical_start + self.raw.separation * (jets_used - 1)
        return WeaveRow(
            source_row=row,
            pass_index=pass_index,
            subpass=subpass,
            nozzle=jet,
            logical_pass_start=start,
            physical_pass_start=physical_start,
            physical_pass_end=physical_end,
            missing_start_rows=missing,
        )


def _sample_codes(plane: NamedRasterPlane, row_index: int) -> np.ndarray:
    codes = np.zeros(plane.width_pixels, dtype=np.uint16)
    for bit_index, bit_plane in enumerate(plane.bit_plane_rows):
        unpacked = np.unpackbits(
            np.frombuffer(bit_plane[row_index], dtype=np.uint8), bitorder="big"
        )[: plane.width_pixels]
        codes |= unpacked.astype(np.uint16) << bit_index
    return codes


def _split_packed_rows(
    codes: Sequence[int] | np.ndarray,
    horizontal_passes: int,
    vertical_passes: int,
    bit_depth: int = 2,
    timings: WeaveStageTimings | None = None,
) -> tuple[bytes, ...]:
    reduced_width = (len(codes) + horizontal_passes - 1) // horizontal_passes
    if bit_depth not in (1, 2):
        raise ValueError("ESC i softweave supports one-bit or two-bit samples")
    packed_bytes = bit_depth * ((reduced_width + 7) // 8)
    outputs: list[bytes] = []
    source = np.asarray(codes, dtype=np.uint16)
    for vertical in range(vertical_passes):
        for horizontal in range(horizontal_passes):
            started = perf_counter()
            selected = np.zeros(reduced_width, dtype=np.uint16)
            source_slice = source[horizontal::horizontal_passes]
            selected[: len(source_slice)] = source_slice
            if vertical_passes > 1:
                nonzero = np.flatnonzero(selected)
                rejected = nonzero[
                    np.arange(len(nonzero), dtype=np.int64) % vertical_passes != vertical
                ]
                selected[rejected] = 0
            if timings is not None:
                timings.vertical_split += perf_counter() - started
            started = perf_counter()
            if bit_depth == 1:
                packed = np.packbits(
                    selected.astype(np.uint8), bitorder="big"
                )
            else:
                padded = np.zeros(packed_bytes * 4, dtype=np.uint16)
                padded[:reduced_width] = selected
                groups = padded.reshape(-1, 4)
                packed = (
                    (groups[:, 0] << 6)
                    | (groups[:, 1] << 4)
                    | (groups[:, 2] << 2)
                    | groups[:, 3]
                ).astype(np.uint8)
            outputs.append(packed.tobytes())
            if timings is not None:
                timings.two_bit_fold += perf_counter() - started
    return tuple(outputs)


def _dense_mapping(
    mapping: dict[int, int] | None,
    stagger: dict[int, int] | None,
) -> tuple[int, np.ndarray, np.ndarray]:
    """Convert the small cooked-weave edge maps to safe dense native LUTs."""

    if not mapping:
        return 0, np.zeros(1, dtype=np.int64), np.zeros(1, dtype=np.int64)
    if stagger is None or set(mapping) != set(stagger):
        raise ValueError("cooked weave map/stagger keys differ")
    first = min(mapping)
    last = max(mapping)
    if set(mapping) != set(range(first, last + 1)):
        raise ValueError("cooked weave map is not dense")
    return (
        first,
        np.asarray([mapping[index] for index in range(first, last + 1)], dtype=np.int64),
        np.asarray([stagger[index] for index in range(first, last + 1)], dtype=np.int64),
    )


@dataclass(slots=True)
class _PassBuffer:
    meta: WeaveRow
    path: PhysicalPathId
    horizontal_position: int
    rows: dict[int, bytes]
    encoded_rows: dict[int, bytes]
    source_rows: dict[int, int]


def _flush_weave_buffers(
    buffers: dict[tuple[int, int, PhysicalPathId], _PassBuffer],
    *,
    before_pass: int | None,
    ordered_paths: list[PhysicalPathId],
    definitions: dict[PhysicalPathId, PhysicalPathDefinition],
    mode: EpsonEscp2ModeParameters,
    reduced_width: int,
    last_pass_offset: int,
    emit: Callable[[WeavePass], None],
) -> int:
    """Complete only passes to which no future source row can contribute."""

    bytes_per_row = mode.bit_depth * ((reduced_width + 7) // 8)
    blank_row = bytes(bytes_per_row)
    blank_encoded = packbits_encode(blank_row)
    pass_keys = sorted(
        {(key[0], key[1]) for key in buffers if before_pass is None or key[0] < before_pass}
    )
    for pass_index, subpass in pass_keys:
        blocks: list[WeaveRasterBlock] = []
        pass_meta: WeaveRow | None = None
        for path in ordered_paths:
            buffer = buffers.pop((pass_index, subpass, path), None)
            if buffer is None or not any(row != blank_row for row in buffer.rows.values()):
                continue
            pass_meta = pass_meta or buffer.meta
            max_nozzle = max(buffer.rows)
            rows = tuple(buffer.rows.get(index, blank_row) for index in range(max_nozzle + 1))
            encoded_rows = tuple(
                buffer.encoded_rows.get(index, blank_encoded)
                for index in range(max_nozzle + 1)
            )
            sources = tuple(buffer.source_rows.get(index) for index in range(max_nozzle + 1))
            active_nozzles = tuple(index for index in range(max_nozzle + 1) if index in buffer.rows)
            raster = PackedRasterPass(
                pass_index=pass_index,
                vertical_advance=0,
                raster_width_pixels=reduced_width,
                decoded_rows=rows,
                address=Escp2InkAddress.from_selector(int(definitions[path].escp2_selector)),
                source_rows=sources,
                nozzle_rows=active_nozzles,
                horizontal_position=buffer.horizontal_position,
                bit_depth=mode.bit_depth,
                encoded_rows=encoded_rows,
            )
            blocks.extend(
                WeaveRasterBlock(
                    path,
                    replace(raster, address=Escp2InkAddress.from_selector(selector)),
                )
                for selector in definitions[path].escp2_selectors
            )
        if not blocks:
            continue
        assert pass_meta is not None
        advance_rows = pass_meta.logical_pass_start - last_pass_offset
        advance_units = advance_rows * mode.vertical_units // mode.printed_vertical_dpi
        advanced_blocks = tuple(
            WeaveRasterBlock(
                item.path,
                replace(item.raster, vertical_advance=advance_units),
            )
            for item in blocks
        )
        emit(
            WeavePass(
                pass_index=pass_index,
                vertical_advance=advance_units,
                logical_pass_start=pass_meta.logical_pass_start,
                physical_pass_start=pass_meta.physical_pass_start,
                physical_pass_end=pass_meta.physical_pass_end,
                subpass=pass_meta.subpass,
                blocks=advanced_blocks,
            )
        )
        last_pass_offset = pass_meta.logical_pass_start
    return last_pass_offset


def _iter_bitplane_bands(
    rasters: PhysicalRasterSet | StreamingPhysicalRasterSet,
    band_rows: int,
):
    """Yield native uint8 bit planes without materializing a page copy."""

    if isinstance(rasters, StreamingPhysicalRasterSet):
        for band in rasters.iter_bands():
            yield band.start_row, tuple(
                (plane.identity, plane.bit_planes) for plane in band.planes
            )
        return

    row_bytes = rasters.width_pixels // 8
    for start in range(0, rasters.height_rows, band_rows):
        end = min(start + band_rows, rasters.height_rows)
        height = end - start
        planes = []
        for plane in rasters.planes:
            bits = np.stack(
                [
                    np.frombuffer(
                        b"".join(bit_plane[start:end]), dtype=np.uint8
                    ).reshape(height, row_bytes)
                    for bit_plane in plane.bit_plane_rows
                ],
                axis=0,
            )
            planes.append((plane.identity, np.ascontiguousarray(bits)))
        yield start, tuple(planes)


def _schedule_single_nozzle_weave(
    rasters: PhysicalRasterSet | StreamingPhysicalRasterSet,
    mode: EpsonEscp2ModeParameters,
    *,
    first_line: int,
    page_height_rows: int,
    left_position_units: int,
    definitions: dict[PhysicalPathId, PhysicalPathDefinition],
    timings: WeaveStageTimings | None = None,
    retain_row_trace: bool = False,
    pass_sink: Callable[[WeavePass], None] | None = None,
    cancel_check: Callable[[], None] | None = None,
) -> WeaveSchedule:
    """Emit firmware-weave rows used by one-nozzle protocol descriptors.

    Several Epson large-format descriptors intentionally declare one logical
    nozzle because the selected ``printerWeave`` is performed by firmware.
    Feeding that sentinel geometry to host softweave makes its top/bottom edge
    remapping degenerate. Each source row is therefore emitted once for every
    horizontal/vertical subpass and the printer performs the physical weave.

    Некоторые широкоформатные descriptors используют ``Nozzles=1`` как
    обозначение аппаратного weave. В этом случае RIP формирует последовательные
    ESC i строки и не придумывает геометрию физической головки.
    """

    hpasses = mode.horizontal_passes
    vpasses = mode.vertical_passes
    subpass_count = hpasses * vpasses
    reduced_width = (rasters.width_pixels + hpasses - 1) // hpasses
    rows: list[tuple[PhysicalPathId, WeaveRow]] = []
    pending: dict[tuple[int, int], list[WeaveRasterBlock]] = {}
    ordered_paths = sorted(
        rasters.identities,
        key=lambda path: definitions[path].allocation_index,
    )
    passes: list[WeavePass] = []
    last_logical_row = 0

    def flush_pending(before_row: int | None) -> None:
        nonlocal last_logical_row
        for logical_row, subpass in sorted(
            key for key in pending if before_row is None or key[0] < before_row
        ):
            blocks = pending.pop((logical_row, subpass))
            advance_rows = logical_row - last_logical_row
            advance_units = advance_rows * mode.vertical_units // mode.printed_vertical_dpi
            pass_index = logical_row * subpass_count + subpass
            advanced_blocks = tuple(
                WeaveRasterBlock(
                    block.path,
                    replace(block.raster, vertical_advance=advance_units),
                )
                for block in blocks
            )
            item = WeavePass(
                pass_index=pass_index,
                vertical_advance=advance_units,
                logical_pass_start=logical_row,
                physical_pass_start=logical_row,
                physical_pass_end=logical_row,
                subpass=subpass,
                blocks=advanced_blocks,
            )
            if pass_sink is None:
                passes.append(item)
            else:
                pass_sink(item)
            last_logical_row = logical_row
    from xp750_rip._cython_kernels import (
        packbits_encode_rows,
        weave_split_1bit_kernel,
        weave_unpack_split_fold_kernel,
    )

    # Firmware-weave printers still need pixel splitting and PackBits, but no
    # host nozzle assignment. Process dense bands in Cython instead of
    # unpacking and repacking every pixel through Python integers.
    band_rows = 512
    for band_start, band_planes in _iter_bitplane_bands(rasters, band_rows):
        if cancel_check is not None:
            cancel_check()
        planes_by_path = dict(band_planes)
        band_height = next(iter(planes_by_path.values())).shape[1]
        for path in ordered_paths:
            bit_planes = planes_by_path[path]
            # Absolute source rows still determine the next paper advance.
            # An empty firmware-weave band needs neither splitting nor PackBits.
            if not retain_row_trace and not np.any(bit_planes):
                continue
            started = perf_counter()
            if mode.bit_depth == 2:
                folded = weave_unpack_split_fold_kernel(
                    bit_planes[0],
                    bit_planes[1],
                    rasters.width_pixels,
                    hpasses,
                    vpasses,
                )
            else:
                folded = weave_split_1bit_kernel(
                    bit_planes[0],
                    rasters.width_pixels,
                    hpasses,
                    vpasses,
                )
            if timings is not None:
                timings.cython_unpack_split_fold += perf_counter() - started

            started = perf_counter()
            encoded = packbits_encode_rows(
                folded.reshape(band_height * subpass_count, folded.shape[2])
            )
            if timings is not None:
                timings.packbits += perf_counter() - started
            active = np.any(folded, axis=2)
            descriptor = definitions[path]
            head_offset = mode.effective_head_offset(descriptor.head_offset)

            for band_index in range(band_height):
                source_index = band_start + band_index
                logical_row = first_line + source_index + head_offset
                for subpass in range(subpass_count):
                    pass_index = logical_row * subpass_count + subpass
                    if retain_row_trace:
                        rows.append(
                            (
                                path,
                                WeaveRow(
                                    source_row=logical_row,
                                    pass_index=pass_index,
                                    subpass=subpass,
                                    nozzle=0,
                                    logical_pass_start=logical_row,
                                    physical_pass_start=logical_row,
                                    physical_pass_end=logical_row,
                                    missing_start_rows=0,
                                ),
                            )
                        )
                    if not active[band_index, subpass]:
                        continue
                    packed = folded[band_index, subpass].tobytes()
                    encoded_row = encoded[band_index * subpass_count + subpass]
                    micro_offset = left_position_units + (subpass % hpasses) * (
                        mode.physical_horizontal_dpi // mode.printed_horizontal_dpi
                    )
                    for selector in descriptor.escp2_selectors:
                        raster = PackedRasterPass(
                            pass_index=pass_index,
                            vertical_advance=0,
                            raster_width_pixels=reduced_width,
                            decoded_rows=(packed,),
                            address=Escp2InkAddress.from_selector(selector),
                            source_rows=(source_index,),
                            nozzle_rows=(0,),
                            horizontal_position=micro_offset,
                            bit_depth=mode.bit_depth,
                            encoded_rows=(encoded_row,),
                        )
                        pending.setdefault((logical_row, subpass), []).append(
                            WeaveRasterBlock(path, raster)
                        )

        if pass_sink is not None:
            next_logical_row = first_line + band_start + band_height + min(
                mode.effective_head_offset(definitions[path].head_offset)
                for path in rasters.identities
            )
            flush_pending(next_logical_row)

    flush_pending(None)
    return WeaveSchedule(tuple(passes), tuple(rows), first_line, page_height_rows)


def schedule_model96_softweave(
    rasters: PhysicalRasterSet | StreamingPhysicalRasterSet,
    mode: EpsonEscp2ModeParameters,
    *,
    first_line: int,
    page_height_rows: int,
    left_position_units: int = 0,
    initial_feed_offset_rows: int = 0,
    path_definitions: tuple[PhysicalPathDefinition, ...] | None = None,
    trace: PerformanceTrace | None = None,
    timings: WeaveStageTimings | None = None,
    retain_row_trace: bool = False,
    pass_sink: Callable[[WeavePass], None] | None = None,
    cancel_check: Callable[[], None] | None = None,
) -> WeaveSchedule:
    """Schedule any data-described Epson path set with host softweave."""

    if rasters.bit_depth != mode.bit_depth:
        raise ValueError("model96 mode and physical raster bit depth differ")
    if initial_feed_offset_rows < 0:
        raise ValueError("initial feed offset cannot be negative")
    if mode.nozzle_count == 1 and initial_feed_offset_rows:
        raise ValueError("initial feed offset requires a host-weave printhead")
    if path_definitions is None:
        definitions = {
            path: PhysicalPathDefinition(
                path,
                descriptor.factory_role.value,
                descriptor.address.selector,
                descriptor.gutenprint_head_offset,
                descriptor.gutenprint_allocation_index,
            )
            for path, descriptor in XP750_PHYSICAL_PATH_BY_ID.items()
        }
    else:
        definitions = {item.identity: item for item in path_definitions}
    unknown = set(rasters.identities) - set(definitions)
    if unknown:
        raise ValueError("ESC/P2 softweave received unknown PhysicalPath")
    if any(definitions[path].escp2_selector is None for path in rasters.identities):
        raise ValueError("ESC/P2 softweave path has no native selector")
    if mode.nozzle_count == 1:
        schedule = _schedule_single_nozzle_weave(
            rasters,
            mode,
            first_line=first_line,
            page_height_rows=page_height_rows,
            left_position_units=left_position_units,
            definitions=definitions,
            timings=timings,
            retain_row_trace=retain_row_trace,
            pass_sink=pass_sink,
            cancel_check=cancel_check,
        )
        if trace is not None:
            trace.event(
                "weave DATA",
                scheduled_passes=len(schedule.passes),
                row_assignments=(
                    rasters.height_rows
                    * len(rasters.identities)
                    * mode.horizontal_passes
                    * mode.vertical_passes
                ),
                hpasses=mode.horizontal_passes,
                vpasses=mode.vertical_passes,
            )
        return schedule
    last_line = first_line + rasters.height_rows - 1 + max(
        mode.effective_head_offset(definitions[path].head_offset)
        for path in rasters.identities
    )
    raw = _RawWeave(
        separation=mode.effective_nozzle_separation,
        jets=mode.nozzle_count,
        oversampling=mode.oversample,
    )
    cooked = _CookedWeave(raw, first_line, last_line, page_height_rows)
    # Horizontal/vertical subpasses may share a logical pass number. Keeping
    # the subpass in the key prevents two independently positioned raster rows
    # from colliding on the same nozzle (notably one-nozzle large-format
    # descriptors at 1440 DPI).
    buffers: dict[tuple[int, int, PhysicalPathId], _PassBuffer] = {}
    row_trace: list[tuple[PhysicalPathId, WeaveRow]] = []
    hpasses = mode.horizontal_passes
    vpasses = mode.vertical_passes
    subpass_count = hpasses * vpasses
    from xp750_rip._cython_kernels import (
        packbits_encode_rows,
        weave_assignment_kernel,
        weave_split_1bit_kernel,
        weave_unpack_split_fold_kernel,
    )

    pre_base, pre_map, pre_stagger = _dense_mapping(cooked.premap, cooked.prestagger)
    post_base, post_map, post_stagger = _dense_mapping(cooked.postmap, cooked.poststagger)
    # In live mode, precompute assignment *metadata*, never pixels.  The
    # suffix minimum proves that a pass below the threshold cannot receive
    # any row from an unseen band, including cooked top/bottom remapping.
    # This is deliberately conservative and makes early emission safe.
    preflight_band_rows = 256
    future_min_pass: list[int] = []
    if pass_sink is not None:
        band_minimums: list[int] = []
        for pre_start in range(0, rasters.height_rows, preflight_band_rows):
            if cancel_check is not None:
                cancel_check()
            pre_height = min(preflight_band_rows, rasters.height_rows - pre_start)
            minimum: int | None = None
            for path in rasters.identities:
                values = weave_assignment_kernel(
                    first_line + pre_start,
                    pre_height,
                    mode.effective_head_offset(definitions[path].head_offset),
                    subpass_count,
                    raw.separation,
                    raw.jets,
                    raw.oversampling,
                    raw.subblocks_per_passblock,
                    raw.passes_per_subblock,
                    cooked.first_premapped,
                    cooked.first_normal,
                    cooked.first_postmapped,
                    cooked.first_row,
                    cooked.last_row,
                    pre_base,
                    pre_map,
                    pre_stagger,
                    post_base,
                    post_map,
                    post_stagger,
                )
                path_minimum = int(values[:, :, 0].min())
                minimum = path_minimum if minimum is None else min(minimum, path_minimum)
            assert minimum is not None
            band_minimums.append(minimum)
        future_min_pass = [2**63 - 1] * (len(band_minimums) + 1)
        for index in range(len(band_minimums) - 1, -1, -1):
            future_min_pass[index] = min(
                band_minimums[index], future_min_pass[index + 1]
            )
    # Large enough to amortize OpenMP setup, while bounding transient buffers
    # for high-DPI multi-channel jobs.
    band_rows = 512
    row_assignment_count = 0
    streamed_pass_count = 0
    # A positive offset advances the sheet before the first raster pass. It
    # leaves every nozzle assignment and subsequent inter-pass advance intact.
    last_pass_offset = -initial_feed_offset_rows
    reduced_width = (rasters.width_pixels + hpasses - 1) // hpasses
    blank_row = bytes(mode.bit_depth * ((reduced_width + 7) // 8))
    blank_encoded = packbits_encode(blank_row)
    blank_subpasses = (blank_row,) * subpass_count
    ordered_paths = sorted(
        rasters.identities,
        key=lambda path: definitions[path].allocation_index,
    )

    def emit_pass(item: WeavePass) -> None:
        nonlocal streamed_pass_count
        streamed_pass_count += 1
        if pass_sink is not None:
            pass_sink(item)
        else:
            passes.append(item)

    passes: list[WeavePass] = []
    for band_start, band_planes in _iter_bitplane_bands(rasters, band_rows):
        if cancel_check is not None:
            cancel_check()
        planes_by_path = dict(band_planes)
        band_height = next(iter(planes_by_path.values())).shape[1]
        band_end = band_start + band_height
        folded_by_path: dict[PhysicalPathId, np.ndarray] = {}
        encoded_by_path: dict[PhysicalPathId, tuple[bytes, ...]] = {}
        assignments: dict[PhysicalPathId, np.ndarray] = {}
        blank_paths: set[PhysicalPathId] = set()
        for path in rasters.identities:
            bit_planes = planes_by_path[path]
            started = perf_counter()
            # Keep every geometric assignment, including blank nozzle rows,
            # while reusing the identical packed representation for empty bands.
            if not np.any(bit_planes):
                blank_paths.add(path)
            elif mode.bit_depth == 2:
                folded_by_path[path] = weave_unpack_split_fold_kernel(
                    bit_planes[0],
                    bit_planes[1],
                    rasters.width_pixels,
                    hpasses,
                    vpasses,
                )
            else:
                folded_by_path[path] = weave_split_1bit_kernel(
                    bit_planes[0],
                    rasters.width_pixels,
                    hpasses,
                    vpasses,
                )
            if timings is not None:
                timings.cython_unpack_split_fold += perf_counter() - started
            started = perf_counter()
            if path in blank_paths:
                encoded_by_path[path] = (blank_encoded,) * (band_height * subpass_count)
            else:
                folded = folded_by_path[path]
                encoded_by_path[path] = packbits_encode_rows(
                    folded.reshape(band_height * subpass_count, folded.shape[2])
                )
            if timings is not None:
                timings.packbits += perf_counter() - started

            descriptor = definitions[path]
            offset = mode.effective_head_offset(descriptor.head_offset)
            started = perf_counter()
            assignments[path] = weave_assignment_kernel(
                first_line + band_start,
                band_height,
                offset,
                subpass_count,
                raw.separation,
                raw.jets,
                raw.oversampling,
                raw.subblocks_per_passblock,
                raw.passes_per_subblock,
                cooked.first_premapped,
                cooked.first_normal,
                cooked.first_postmapped,
                cooked.first_row,
                cooked.last_row,
                pre_base,
                pre_map,
                pre_stagger,
                post_base,
                post_map,
                post_stagger,
            )
            if timings is not None:
                timings.cython_assignment += perf_counter() - started

        for source_index in range(band_start, band_end):
            band_index = source_index - band_start
            for path in rasters.identities:
                if path in blank_paths:
                    packed_subpasses = blank_subpasses
                else:
                    folded = folded_by_path[path]
                    packed_subpasses = tuple(
                        folded[band_index, index].tobytes()
                        for index in range(subpass_count)
                    )
                encoded_subpasses = encoded_by_path[path][
                    band_index * subpass_count : (band_index + 1) * subpass_count
                ]
                plane_assignments = assignments[path]
                for subpass, (packed, encoded) in enumerate(
                    zip(packed_subpasses, encoded_subpasses, strict=True)
                ):
                    values = plane_assignments[band_index, subpass]
                    row_assignment_count += 1
                    pass_index = int(values[0])
                    nozzle = int(values[1])
                    micro_offset = left_position_units + (subpass & (hpasses - 1)) * (
                        mode.physical_horizontal_dpi // mode.printed_horizontal_dpi
                    )
                    key = (pass_index, subpass, path)
                    buffer = buffers.get(key)
                    if buffer is None:
                        assignment = WeaveRow(
                            source_row=first_line
                            + source_index
                            + mode.effective_head_offset(
                                definitions[path].head_offset
                            ),
                            pass_index=pass_index,
                            subpass=subpass,
                            nozzle=nozzle,
                            logical_pass_start=int(values[2]),
                            physical_pass_start=int(values[3]),
                            physical_pass_end=int(values[4]),
                            missing_start_rows=int(values[5]),
                        )
                        buffer = _PassBuffer(
                            meta=assignment,
                            path=path,
                            horizontal_position=micro_offset,
                            rows={},
                            encoded_rows={},
                            source_rows={},
                        )
                        buffers[key] = buffer
                    elif retain_row_trace:
                        assignment = WeaveRow(
                            source_row=first_line
                            + source_index
                            + mode.effective_head_offset(
                                definitions[path].head_offset
                            ),
                            pass_index=pass_index,
                            subpass=subpass,
                            nozzle=nozzle,
                            logical_pass_start=int(values[2]),
                            physical_pass_start=int(values[3]),
                            physical_pass_end=int(values[4]),
                            missing_start_rows=int(values[5]),
                        )
                    if retain_row_trace:
                        row_trace.append((path, assignment))
                    if nozzle in buffer.rows:
                        raise ValueError(
                            "model96 softweave assigned two source rows to one pass/nozzle"
                        )
                    buffer.rows[nozzle] = packed
                    buffer.encoded_rows[nozzle] = encoded
                    buffer.source_rows[nozzle] = source_index

        if pass_sink is not None:
            future_block = band_end // preflight_band_rows
            threshold = future_min_pass[future_block]
            last_pass_offset = _flush_weave_buffers(
                buffers,
                before_pass=threshold,
                ordered_paths=ordered_paths,
                definitions=definitions,
                mode=mode,
                reduced_width=reduced_width,
                last_pass_offset=last_pass_offset,
                emit=emit_pass,
            )

    last_pass_offset = _flush_weave_buffers(
        buffers,
        before_pass=None,
        ordered_paths=ordered_paths,
        definitions=definitions,
        mode=mode,
        reduced_width=reduced_width,
        last_pass_offset=last_pass_offset,
        emit=emit_pass,
    )
    schedule = WeaveSchedule(tuple(passes), tuple(row_trace), first_line, page_height_rows)
    if trace is not None:
        trace.event(
            "weave DATA",
            scheduled_passes=streamed_pass_count,
            row_assignments=row_assignment_count,
            hpasses=hpasses,
            vpasses=vpasses,
        )
    return schedule
