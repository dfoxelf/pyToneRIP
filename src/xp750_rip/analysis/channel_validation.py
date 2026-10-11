# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Offline structural and binary validation for Phase 4 channel candidates."""

from dataclasses import dataclass
from hashlib import sha256
from itertools import combinations

from xp750_rip.analysis.escp2_parser import EvidenceClass, StreamRecord, parse_stream
from xp750_rip.channels import PhysicalChannel
from xp750_rip.escp2.channels import Escp2InkAddress
from xp750_rip.escp2.job import GeneratedChannelTestJob


@dataclass(frozen=True, slots=True)
class BinaryDifference:
    offset: int
    left: int
    right: int


@dataclass(frozen=True, slots=True)
class ChannelPairComparison:
    left: PhysicalChannel
    right: PhysicalChannel
    differences: tuple[BinaryDifference, ...]
    allowed_selector_offsets: tuple[int, ...]

    @property
    def only_selector_differs(self) -> bool:
        return tuple(item.offset for item in self.differences) == self.allowed_selector_offsets


@dataclass(frozen=True, slots=True)
class ValidatedChannelJob:
    job: GeneratedChannelTestJob
    records: tuple[StreamRecord, ...]
    selector_offsets: tuple[int, ...]
    decoded_raster_sha256: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Phase3DBaselineComparison:
    differences: tuple[BinaryDifference, ...]
    color_configuration_offset: int
    selector_offsets: tuple[int, ...]

    @property
    def only_authorized_semantic_fields_differ(self) -> bool:
        allowed = {self.color_configuration_offset, *self.selector_offsets}
        return all(item.offset in allowed for item in self.differences)


def validate_channel_job(job: GeneratedChannelTestJob) -> ValidatedChannelJob:
    """Parse the entire candidate and enforce the fixed Phase 4 invariants."""

    records = tuple(parse_stream(job.data))
    if not records or records[-1].end_offset != len(job.data):
        raise ValueError("Phase 4 candidate was not consumed completely")
    if any(record.confidence is EvidenceClass.UNKNOWN for record in records):
        raise ValueError("Phase 4 candidate contains an UNKNOWN parser record")

    color_modes = [record for record in records if record.name == "GRAY_MODE"]
    if len(color_modes) != 1 or color_modes[0].parameters["payload_hex"] != "00 02":
        raise ValueError("Phase 4 requires Gutenprint model-96 color configuration 2")

    raster_records = [record for record in records if record.raster is not None]
    if len(raster_records) != 2:
        raise ValueError("Phase 4 requires exactly two raster passes")

    decoded_hashes: list[str] = []
    selector_offsets: list[int] = []
    for source_pass, record in zip(job.passes, raster_records, strict=True):
        raster = record.raster
        if raster is None:
            raise AssertionError("raster record unexpectedly has no raster data")
        if raster.selector != job.mapping.address.selector:
            raise ValueError("raster selector does not match the intended source mapping")
        if Escp2InkAddress.from_selector(raster.selector) != job.mapping.address:
            raise ValueError("raster address does not round-trip through ESC i")
        if raster.bit_depth != 2 or raster.compression != 1 or raster.rows != 180:
            raise ValueError("raster mechanics differ from the fixed Phase 4 profile")
        if raster.decoded_rows != source_pass.decoded_rows:
            raise ValueError("parsed raster does not reconstruct the generated planes")
        if any(
            any(row[source_pass.bytes_per_plane_row :]) for row in raster.decoded_rows
        ):
            raise ValueError("the second bit plane must remain empty")
        selector_offsets.append(record.offset + 2)
        decoded_hashes.append(sha256(raster.decoded_payload).hexdigest())

    return ValidatedChannelJob(
        job=job,
        records=records,
        selector_offsets=tuple(selector_offsets),
        decoded_raster_sha256=tuple(decoded_hashes),
    )


def compare_channel_jobs(
    validated_jobs: tuple[ValidatedChannelJob, ...],
) -> tuple[ChannelPairComparison, ...]:
    """Report every byte difference for all 15 pairs of six candidates."""

    if len(validated_jobs) != 6:
        raise ValueError("the Phase 4 comparison requires all six channel candidates")
    results: list[ChannelPairComparison] = []
    for left, right in combinations(validated_jobs, 2):
        if len(left.job.data) != len(right.job.data):
            raise ValueError("Phase 4 candidates have different lengths")
        if left.selector_offsets != right.selector_offsets:
            raise ValueError("raster selector offsets differ between candidates")
        differences = tuple(
            BinaryDifference(offset, left_byte, right_byte)
            for offset, (left_byte, right_byte) in enumerate(
                zip(left.job.data, right.job.data, strict=True)
            )
            if left_byte != right_byte
        )
        comparison = ChannelPairComparison(
            left.job.mapping.physical_channel,
            right.job.mapping.physical_channel,
            differences,
            left.selector_offsets,
        )
        if not comparison.only_selector_differs:
            raise ValueError(
                f"unexpected binary difference: {comparison.left}/{comparison.right}"
            )
        results.append(comparison)
    return tuple(results)


def compare_to_phase3d_baseline(
    baseline_data: bytes, candidate: ValidatedChannelJob
) -> Phase3DBaselineComparison:
    """Prove that Phase 3D mechanics changed only for required channel semantics."""

    if len(baseline_data) != len(candidate.job.data):
        raise ValueError("Phase 4 candidate length differs from the Phase 3D baseline")
    baseline_records = tuple(parse_stream(baseline_data))
    if tuple(record.name for record in baseline_records) != tuple(
        record.name for record in candidate.records
    ):
        raise ValueError("Phase 4 command sequence differs from the Phase 3D baseline")
    gray_records = [record for record in baseline_records if record.name == "GRAY_MODE"]
    if len(gray_records) != 1:
        raise ValueError("Phase 3D baseline does not contain exactly one color configuration")
    color_offset = gray_records[0].offset + 6
    differences = tuple(
        BinaryDifference(offset, baseline_byte, candidate_byte)
        for offset, (baseline_byte, candidate_byte) in enumerate(
            zip(baseline_data, candidate.job.data, strict=True)
        )
        if baseline_byte != candidate_byte
    )
    result = Phase3DBaselineComparison(
        differences=differences,
        color_configuration_offset=color_offset,
        selector_offsets=candidate.selector_offsets,
    )
    if not result.only_authorized_semantic_fields_differ:
        raise ValueError("Phase 4 changed a mechanically relevant Phase 3D byte")
    if color_offset not in {item.offset for item in differences}:
        raise ValueError("Phase 4 did not select the required PhotoCMYK configuration")
    return result


def format_channel_comparisons(comparisons: tuple[ChannelPairComparison, ...]) -> str:
    """Render an exhaustive Russian byte-difference report."""

    lines = [
        "Phase 4 — попарное бинарное сравнение",
        "",
        "Сравнены все 15 пар. Смещение указывает на байт selector в ESC i.",
        "Других бинарных различий нет.",
        "",
        "ПАРА       РАЗЛИЧИЯ",
    ]
    for item in comparisons:
        differences = ", ".join(
            f"0x{difference.offset:06X}:0x{difference.left:02X}->0x{difference.right:02X}"
            for difference in item.differences
        )
        lines.append(f"{item.left.value:>2}/{item.right.value:<2}      {differences}")
    return "\n".join(lines) + "\n"
