# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Offline structural validation for Phase 5 multi-path candidates."""

from dataclasses import dataclass
from hashlib import sha256

from xp750_rip.analysis.escp2_parser import (
    EvidenceClass,
    StreamRecord,
    format_stream,
    parse_stream,
)
from xp750_rip.escp2.job import GeneratedMultipathJob, MultipathRasterBlock
from xp750_rip.escp2.packbits import encode as packbits_encode
from xp750_rip.raster import PhysicalPathId


@dataclass(frozen=True, slots=True)
class MultipathBlockValidation:
    pass_index: int
    path: PhysicalPathId
    selector: int
    record_offset: int
    vertical_position: int
    rows: int
    width_pixels: int
    active_bits: tuple[int, ...]
    decoded_sha256: str


@dataclass(frozen=True, slots=True)
class ValidatedMultipathJob:
    job: GeneratedMultipathJob
    records: tuple[StreamRecord, ...]
    blocks: tuple[MultipathBlockValidation, ...]
    vertical_advances: tuple[int, ...]
    vertical_positions: tuple[int, ...]

    @property
    def selector_sequence(self) -> tuple[int, ...]:
        return tuple(block.selector for block in self.blocks)

    @property
    def raster_block_count(self) -> int:
        return len(self.blocks)


def _expected_page_record_names(job: GeneratedMultipathJob) -> tuple[str, ...]:
    names: list[str] = []
    for scheduled_pass in job.passes:
        names.append("RELATIVE_VERTICAL")
        for _ in scheduled_pass.blocks:
            names.extend(("RASTER_IMAGE", "CARRIAGE_RETURN"))
    return tuple(names)


def _flatten_blocks(job: GeneratedMultipathJob) -> tuple[MultipathRasterBlock, ...]:
    return tuple(block for scheduled_pass in job.passes for block in scheduled_pass.blocks)


def validate_multipath_job(job: GeneratedMultipathJob) -> ValidatedMultipathJob:
    """Enforce parser, schedule, raster, selector, and termination invariants."""

    records = tuple(parse_stream(job.data))
    if not records or records[-1].end_offset != len(job.data):
        raise ValueError("Phase 5 candidate was not consumed completely")
    if any(record.confidence is EvidenceClass.UNKNOWN for record in records):
        raise ValueError("Phase 5 candidate contains an UNKNOWN parser record")

    color_modes = [record for record in records if record.name == "GRAY_MODE"]
    if len(color_modes) != 1 or color_modes[0].parameters["payload_hex"] != "00 02":
        raise ValueError("Phase 5 requires Gutenprint model-96 color configuration 2")

    print_method_index = next(
        (index for index, record in enumerate(records) if record.name == "PRINT_METHOD"),
        None,
    )
    form_feed_index = next(
        (index for index, record in enumerate(records) if record.name == "FORM_FEED"),
        None,
    )
    if print_method_index is None or form_feed_index is None:
        raise ValueError("Phase 5 stream is missing page framing")
    page_names = tuple(
        record.name for record in records[print_method_index + 1 : form_feed_index]
    )
    if page_names != _expected_page_record_names(job):
        raise ValueError("parsed multi-path page ordering differs from the scheduled passes")

    vertical_records = [record for record in records if record.name == "RELATIVE_VERTICAL"]
    vertical_advances = tuple(int(record.parameters["value"]) for record in vertical_records)
    if vertical_advances != tuple(item.vertical_advance for item in job.passes):
        raise ValueError("parsed vertical advances differ from the pass schedule")
    vertical_positions: list[int] = []
    position = 0
    for advance in vertical_advances:
        position += advance
        vertical_positions.append(position)

    raster_records = tuple(record for record in records if record.raster is not None)
    source_blocks = _flatten_blocks(job)
    if len(raster_records) != len(source_blocks):
        raise ValueError("parsed raster block count differs from the pass schedule")

    validations: list[MultipathBlockValidation] = []
    block_index = 0
    for pass_index, scheduled_pass in enumerate(job.passes):
        for source_block in scheduled_pass.blocks:
            record = raster_records[block_index]
            raster = record.raster
            if raster is None:
                raise AssertionError("raster record unexpectedly has no raster data")
            source = source_block.raster
            if raster.selector != source.selector:
                raise ValueError("parsed selector differs from the scheduled physical path")
            if (
                raster.compression,
                raster.bit_depth,
                raster.rows,
                raster.bytes_per_row,
            ) != (
                source.compression,
                source.bit_depth,
                source.row_count,
                source.bytes_per_raster_row,
            ):
                raise ValueError("parsed raster mechanics differ from the scheduled block")
            if raster.decoded_rows != source.decoded_rows:
                raise ValueError("PackBits decode did not reconstruct the scheduled raster")
            if tuple(packbits_encode(row) for row in raster.decoded_rows) != source.compressed_rows:
                raise ValueError("PackBits re-encode differs from the generated raster rows")

            plane_bytes = source.bytes_per_plane_row
            active_bits = (
                sum(byte.bit_count() for row in raster.decoded_rows for byte in row[:plane_bytes]),
                sum(byte.bit_count() for row in raster.decoded_rows for byte in row[plane_bytes:]),
            )
            if active_bits[0] <= 0 or active_bits[1] != 0:
                raise ValueError("Phase 5 requires active smallest-dot data and an empty second plane")
            validations.append(
                MultipathBlockValidation(
                    pass_index=pass_index,
                    path=source_block.path,
                    selector=raster.selector,
                    record_offset=record.offset,
                    vertical_position=vertical_positions[pass_index],
                    rows=raster.rows,
                    width_pixels=raster.pixels_per_row or 0,
                    active_bits=active_bits,
                    decoded_sha256=sha256(raster.decoded_payload).hexdigest(),
                )
            )
            block_index += 1

    expected_paths = set(job.active_paths)
    for pass_index in range(len(job.passes)):
        observed = {item.path for item in validations if item.pass_index == pass_index}
        if observed != expected_paths:
            raise ValueError("a pass does not contain the exact active physical path set")
    source_hashes = {
        sha256(b"".join(plane.decoded_rows)).hexdigest() for plane in job.rasters.planes
    }
    if len(source_hashes) != len(job.rasters.planes):
        raise ValueError("physical paths do not contain independent raster data")

    if [record.name for record in records[-6:]] != [
        "FORM_FEED",
        "RESET",
        "REMOTE_MODE_ENTER",
        "REMOTE_LD",
        "REMOTE_JE",
        "REMOTE_MODE_EXIT",
    ]:
        raise ValueError("Phase 5 page/job termination differs from the verified baseline")

    return ValidatedMultipathJob(
        job=job,
        records=records,
        blocks=tuple(validations),
        vertical_advances=vertical_advances,
        vertical_positions=tuple(vertical_positions),
    )


def format_multipath_analysis(validated: ValidatedMultipathJob) -> str:
    """Render validation summary and the complete parsed command sequence in Russian."""

    lines = [
        "Phase 5 — офлайн-разбор multi-path ESC/P2 job",
        "",
        f"Размер PRN: {len(validated.job.data)} байт",
        f"SHA-256: {validated.job.sha256}",
        f"Активные PhysicalPaths: {', '.join(path.value for path in validated.job.active_paths)}",
        f"Число raster blocks: {validated.raster_block_count}",
        "Selector sequence: "
        + ", ".join(f"0x{selector:02X}" for selector in validated.selector_sequence),
        f"Vertical advances: {list(validated.vertical_advances)}",
        f"Vertical positions: {list(validated.vertical_positions)}",
        "PackBits round-trip: PASS",
        "Полное потребление потока: PASS",
        "Page/job termination: PASS",
        "",
        "РАСТРОВЫЕ БЛОКИ",
    ]
    for item in validated.blocks:
        lines.append(
            f"pass={item.pass_index} path={item.path.value} selector=0x{item.selector:02X} "
            f"offset=0x{item.record_offset:06X} vertical={item.vertical_position} "
            f"rows={item.rows} width={item.width_pixels} active_bits={list(item.active_bits)} "
            f"decoded_sha256={item.decoded_sha256}"
        )
    lines.extend(("", "ПОЛНАЯ ПОСЛЕДОВАТЕЛЬНОСТЬ КОМАНД", ""))
    lines.append(format_stream(list(validated.records)).rstrip("\n"))
    return "\n".join(lines) + "\n"
