# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Offline structural and raster-exact validation for the Phase 7 image job."""

from __future__ import annotations

from dataclasses import dataclass

from xp750_rip.analysis.escp2_parser import (
    EvidenceClass,
    StreamRecord,
    format_stream,
    parse_stream,
)
from xp750_rip.escp2.job import GeneratedPhysicalRasterJob
from xp750_rip.escp2.packbits import encode as packbits_encode


@dataclass(frozen=True, slots=True)
class Phase7PrnValidation:
    records: tuple[StreamRecord, ...]
    selector_sequence: tuple[int, ...]
    vertical_advances: tuple[int, ...]
    vertical_positions: tuple[int, ...]
    raster_block_count: int
    reconstructed_dot_plane_matches: bool


def validate_phase7_image_job(job: GeneratedPhysicalRasterJob) -> Phase7PrnValidation:
    records = tuple(parse_stream(job.data))
    if not records or records[-1].end_offset != len(job.data):
        raise ValueError("Phase 7 PRN was not consumed completely")
    if any(record.confidence is EvidenceClass.UNKNOWN for record in records):
        raise ValueError("Phase 7 PRN contains an UNKNOWN parser record")
    if len(job.rasters.planes) != 1:
        raise ValueError("Phase 7 first-image job must contain exactly one physical path")
    if len(job.passes) != 2 or any(len(item.blocks) != 1 for item in job.passes):
        raise ValueError("Phase 7 image must activate one block in both verified passes")

    color_modes = [record for record in records if record.name == "GRAY_MODE"]
    if len(color_modes) != 1 or color_modes[0].parameters["payload_hex"] != "00 02":
        raise ValueError("Phase 7 requires verified model-96 color configuration 2")

    vertical_records = [record for record in records if record.name == "RELATIVE_VERTICAL"]
    vertical_advances = tuple(int(record.parameters["value"]) for record in vertical_records)
    expected_advances = tuple(item.vertical_advance for item in job.passes)
    if vertical_advances != expected_advances:
        raise ValueError("parsed Phase 7 vertical advances differ from the schedule")
    positions: list[int] = []
    position = 0
    for advance in vertical_advances:
        position += advance
        positions.append(position)

    raster_records = tuple(record for record in records if record.raster is not None)
    blocks = tuple(block for item in job.passes for block in item.blocks)
    if len(raster_records) != len(blocks):
        raise ValueError("parsed Phase 7 raster block count differs from the schedule")
    source_plane = job.rasters.planes[0]
    reconstructed = [[], []]
    selectors: list[int] = []
    plane_bytes = source_plane.width_pixels // 8
    for record, block in zip(raster_records, blocks, strict=True):
        parsed = record.raster
        if parsed is None:
            raise AssertionError("raster record unexpectedly has no decoded payload")
        source = block.raster
        if (
            parsed.selector,
            parsed.compression,
            parsed.bit_depth,
            parsed.rows,
            parsed.bytes_per_row,
        ) != (
            source.selector,
            source.compression,
            source.bit_depth,
            source.row_count,
            source.bytes_per_raster_row,
        ):
            raise ValueError("parsed Phase 7 raster mechanics differ from scheduled data")
        if parsed.decoded_rows != source.decoded_rows:
            raise ValueError("Phase 7 PackBits decode differs from scheduled raster rows")
        if tuple(packbits_encode(row) for row in parsed.decoded_rows) != source.compressed_rows:
            raise ValueError("Phase 7 PackBits re-encode differs from generated rows")
        selectors.append(parsed.selector)
        reconstructed[0].extend(row[:plane_bytes] for row in parsed.decoded_rows)
        reconstructed[1].extend(row[plane_bytes:] for row in parsed.decoded_rows)

    reconstructed_tuple = tuple(tuple(rows) for rows in reconstructed)
    if reconstructed_tuple != source_plane.bit_plane_rows:
        raise ValueError("PRN raster reconstruction differs from source physical dot plane")
    expected_selector = next(item.blocks[0].raster.selector for item in job.passes)
    if set(selectors) != {expected_selector}:
        raise ValueError("Phase 7 PRN contains an unexpected selector")
    if [record.name for record in records[-6:]] != [
        "FORM_FEED",
        "RESET",
        "REMOTE_MODE_ENTER",
        "REMOTE_LD",
        "REMOTE_JE",
        "REMOTE_MODE_EXIT",
    ]:
        raise ValueError("Phase 7 page/job termination differs from the verified backend")
    return Phase7PrnValidation(
        records=records,
        selector_sequence=tuple(selectors),
        vertical_advances=vertical_advances,
        vertical_positions=tuple(positions),
        raster_block_count=len(raster_records),
        reconstructed_dot_plane_matches=True,
    )


def format_phase7_analysis(
    job: GeneratedPhysicalRasterJob, validation: Phase7PrnValidation
) -> str:
    lines = [
        "Phase 7 — офлайн-разбор первого TIFF/QTR image PRN",
        "",
        f"Размер PRN: {len(job.data)} байт",
        f"SHA-256: {job.sha256}",
        "PhysicalPath: " + job.active_paths[0].value,
        "Selector sequence: "
        + ", ".join(f"0x{selector:02X}" for selector in validation.selector_sequence),
        f"Vertical advances: {list(validation.vertical_advances)}",
        f"Vertical positions: {list(validation.vertical_positions)}",
        f"Raster blocks: {validation.raster_block_count}",
        "PackBits round-trip: PASS",
        "Реконструкция physical dot plane: EXACT MATCH",
        "Полное потребление потока: PASS",
        "UNKNOWN records: 0",
        "Page/job termination: PASS",
        "",
        "ПОЛНАЯ ПОСЛЕДОВАТЕЛЬНОСТЬ КОМАНД",
        "",
        format_stream(list(validation.records)).rstrip("\n"),
    ]
    return "\n".join(lines) + "\n"
