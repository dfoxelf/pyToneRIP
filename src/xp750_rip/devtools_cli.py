# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Исторические file-only generators и diagnostics для ``epson-rip dev``."""

import argparse
import json
from pathlib import Path

from xp750_rip import __version__
from xp750_rip.analysis.channel_validation import (
    compare_channel_jobs,
    compare_to_phase3d_baseline,
    format_channel_comparisons,
    validate_channel_job,
)
from xp750_rip.analysis.escp2_parser import format_stream, parse_stream
from xp750_rip.analysis.gutenprint_reference import summarize_raster, verify_reference
from xp750_rip.analysis.multipath_validation import (
    ValidatedMultipathJob,
    format_multipath_analysis,
    validate_multipath_job,
)
from xp750_rip.analysis.semantic_compare import (
    compare_phase3c_to_golden,
    format_semantic_comparison,
)
from xp750_rip.channels import PhysicalChannel
from xp750_rip.escp2.job import (
    generate_channel_test_job,
    generate_phase2_job,
    generate_phase3c_job,
    generate_xp750_multipath_test_job,
)
from xp750_rip.escp2.model import PX700W_BASELINE
from xp750_rip.escp2.parser import format_analysis, parse
from xp750_rip.phase7 import make_phase7_first_image
from xp750_rip.printers.xp750 import (
    XP750_PHASE3C,
    XP750_PHYSICAL_PATH_BY_ID,
    XP750_RECOMMENDED_TEST_ORDER,
    XP750_SOURCE_CHANNELS,
    SourceDerivedChannel,
)
from xp750_rip.qtr.diagnostics import generate_qtr_analysis
from xp750_rip.testpatterns.patterns import IdentificationPattern

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_GOLDEN_REFERENCE = PROJECT_ROOT / "reference" / "model96_reference_5.3.4.prn"
GOLDEN_SHA256 = "8e7ca477a0754e7919c68e55188334f7e186448e52daab013d97148cb6121ee0"

PHASE4_SOURCE_EVIDENCE = [
    "gutenprint-5.3.5/src/xml/printers/escp2.xml:418 (PX700W -> model 96)",
    "gutenprint-5.3.5/src/xml/escp2/model/model_96.xml:22-74",
    "gutenprint-5.3.5/src/xml/escp2/inks/artisan.xml:22-68 (PhotoCMYK/CcMmYK)",
    "gutenprint-5.3.5/src/main/escp2-channels.c:33-296",
    "gutenprint-5.3.5/src/main/print-escp2.c:3839-3892,3994-4136",
    "gutenprint-5.3.5/src/main/escp2-driver.c:339-347,791-802",
    "gutenprint-5.3.5/doc/developer/escp2.xml:503-560",
]

PHASE5_SOURCE_EVIDENCE = [
    {
        "classification": "FACT",
        "source": "gutenprint-5.3.5/src/xml/escp2/inks/artisan.xml:33-68",
        "finding": "Поля PhotoCMYK color/subchannel/headOffset для K/C/LC/M/LM/Y",
    },
    {
        "classification": "FACT",
        "source": "gutenprint-5.3.5/src/xml/escp2/model/base/baseline_360.xml:23",
        "finding": "model 96 наследует baseSeparation=360",
    },
    {
        "classification": "FACT",
        "source": "gutenprint-5.3.5/src/main/print-escp2.c:3672-3723",
        "finding": (
            "setup_head_offset копирует headOffset и целочисленно масштабирует его "
            "через vres/baseSeparation"
        ),
    },
    {
        "classification": "FACT",
        "source": "gutenprint-5.3.5/src/main/print-escp2.c:3839-3892",
        "finding": "allocate_channels обходит logical channel, затем subchannel",
    },
    {
        "classification": "FACT",
        "source": "gutenprint-5.3.5/src/main/print-weave.c:1002-1130,1602-1710",
        "finding": ("softweave применяет per-channel offsets и хранит активность lines по pass"),
    },
    {
        "classification": "FACT",
        "source": "gutenprint-5.3.5/src/main/escp2-driver.c:503-525,685-802",
        "finding": ("flush выдаёт одну подачу pass и отдельную запись ESC i/CR на active channel"),
    },
    {
        "classification": "FACT",
        "source": "соответствующие файлы gutenprint-master",
        "finding": "Целевая логика head offset и multi-channel flush не изменилась",
    },
]

_PHASE5_PATTERN_RUSSIAN = {
    IdentificationPattern.VERTICAL_LINE: "вертикальная линия",
    IdentificationPattern.HORIZONTAL_LINE: "горизонтальная линия",
    IdentificationPattern.BACKSLASH: "диагональ \\",
    IdentificationPattern.SLASH: "диагональ /",
    IdentificationPattern.RECTANGLE: "малый прямоугольник",
    IdentificationPattern.CROSS: "крест +",
}


def make_test_prn(output_dir: Path) -> tuple[Path, Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    job = generate_phase2_job()
    model = PX700W_BASELINE
    prn_path = output_dir / "phase2_test.prn"
    manifest_path = output_dir / "phase2_test.json"
    analysis_path = output_dir / "phase2_test_analysis.txt"
    prn_path.write_bytes(job.data)
    manifest = {
        "target_printer": model.target_name,
        "reference_model": model.reference_name,
        "gutenprint_driver": model.gutenprint_driver,
        "gutenprint_model": model.gutenprint_model,
        "gutenprint_parameters": model.gutenprint_parameters,
        "resolution_dpi": [model.horizontal_dpi, model.vertical_dpi],
        "page_size": "A4",
        "page_dimensions_mm": [210, 297],
        "printable_dimensions_points": [594, 842],
        "channel": {
            "name": model.black.name,
            "selector": model.black.selector,
            "subchannel": model.black.subchannel,
            "expected_physical_path": model.black.expected_ink_path,
            "confidence": model.black.confidence,
        },
        "bit_depth": model.bit_depth,
        "dot_pattern": 1,
        "compression": model.compression,
        "test_pattern": job.geometry.as_dict(),
        "position_units": [job.horizontal_position, job.vertical_position],
        "prn_byte_length": len(job.data),
        "sha256": job.sha256,
        "implementation_version": __version__,
        "unresolved_assumptions": [
            "XP-750 mechanical/media equivalence beyond the user-verified PX700W baseline is UNKNOWN.",
            "No Phase 2 hardware print confirms selector 0 reaches the installed black path.",
            "This minimal single-block stream does not reproduce Gutenprint softweave pass scheduling.",
            "The 0x31 model-resolution ink/drop selector is source-derived but not experimentally confirmed.",
        ],
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    analysis_path.write_text(format_analysis(parse(job.data)), encoding="utf-8")
    return prn_path, manifest_path, analysis_path


def make_phase3c_prn(
    output_dir: Path,
    golden_reference: Path = DEFAULT_GOLDEN_REFERENCE,
) -> tuple[Path, Path, Path, Path]:
    """Generate Phase 3C first, then use the immutable golden file only for validation."""

    job = generate_phase3c_job()
    candidate_records = tuple(parse_stream(job.data))
    if not candidate_records or candidate_records[-1].end_offset != len(job.data):
        raise RuntimeError("Phase 3C stream was not consumed completely by the parser")

    verified_golden = verify_reference(golden_reference, GOLDEN_SHA256)
    comparison = compare_phase3c_to_golden(verified_golden.records, candidate_records)
    if comparison.has_required_fixes:
        raise RuntimeError("Phase 3C semantic comparison contains REQUIRES_FIX")

    output_dir.mkdir(parents=True, exist_ok=True)
    prn_path = output_dir / "phase3c_test.prn"
    manifest_path = output_dir / "phase3c_test.json"
    analysis_path = output_dir / "phase3c_test_analysis.txt"
    comparison_path = output_dir / "phase3c_vs_golden.txt"

    prn_path.write_bytes(job.data)
    parsed_rasters = [record.raster for record in candidate_records if record.raster is not None]
    raster_summaries = [summarize_raster(raster) for raster in parsed_rasters]
    profile = XP750_PHASE3C
    manifest = {
        "target_device": profile.target_device,
        "reference_model": profile.reference_model,
        "gutenprint_driver": profile.gutenprint_driver,
        "gutenprint_model": profile.gutenprint_model,
        "gutenprint_parameters": profile.gutenprint_parameters,
        "resolution": {
            "name": profile.resolution_name,
            "horizontal_dpi": profile.horizontal_dpi,
            "vertical_dpi": profile.vertical_dpi,
            "esc_u_unit_scale": profile.unit_scale,
            "esc_d_resolution_scale": profile.printhead_resolution_scale,
        },
        "media": profile.media,
        "input_slot": profile.input_slot,
        "color_mode": profile.color_mode,
        "ink_type": profile.ink_type,
        "print_method": "0x10",
        "escp2_selector": "0x00",
        "bit_depth": profile.bit_depth,
        "compression": {"id": profile.compression, "name": "TIFF PackBits"},
        "page_geometry": {
            "name": profile.page.name,
            "width_inches": profile.page.width_inches,
            "height_inches": profile.page.height_inches,
            "width_page_units": profile.page.width_page_units,
            "height_page_units": profile.page.height_page_units,
            "format_top_vertical_units": profile.page.format_top_vertical_units,
            "format_bottom_vertical_units": profile.page.format_bottom_vertical_units,
        },
        "pass_count": len(job.passes),
        "rows_per_pass": [raster_pass.row_count for raster_pass in job.passes],
        "raster_width_pixels": profile.raster_width_pixels,
        "raster_bytes_per_row": profile.bytes_per_raster_row,
        "vertical_advance_per_pass": [raster_pass.vertical_advance for raster_pass in job.passes],
        "pattern": job.geometry.as_dict(),
        "pattern_bounds": list(job.geometry.bounds),
        "passes": [
            {
                "index": raster_pass.pass_index,
                "selector": "0x00",
                "rows": raster_pass.row_count,
                "compressed_payload_bytes": len(raster_pass.compressed_payload),
                "plane0_active_bits": summary.planes[0].active_bits,
                "plane0_bounds": summary.planes[0].bounding_box,
                "plane1_active_bits": summary.planes[1].active_bits,
            }
            for raster_pass, summary in zip(job.passes, raster_summaries, strict=True)
        ],
        "prn_size": len(job.data),
        "sha256": job.sha256,
        "implementation_version": __version__,
        "semantic_comparison": {
            "critical_requires_fix": comparison.has_required_fixes,
            "golden_sha256": verified_golden.sha256,
        },
        "unresolved_assumptions": [
            "UNKNOWN: минимально обязательное подмножество REMOTE1 не проверено отдельно.",
            "UNKNOWN: точная семантика JS, SN и JE установлена не полностью.",
            "UNKNOWN: selector 0x00 не доказывает внутренний номер nozzle bank XP-750.",
            "UNKNOWN: механическое отображение двух pass на бумагу будет подтверждено только отдельным контролируемым тестом.",
        ],
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    analysis_path.write_text(format_stream(list(candidate_records)), encoding="utf-8")
    comparison_path.write_text(format_semantic_comparison(comparison), encoding="utf-8")
    return prn_path, manifest_path, analysis_path, comparison_path


def _phase4_mapping_dict(mapping: SourceDerivedChannel) -> dict[str, object]:
    return {
        "intended_physical_channel": mapping.physical_channel.value,
        "source_expected": mapping.physical_channel.value,
        "physical_observed": None,
        "physical_status": mapping.physical_test_status.value,
        "logical_channel": mapping.logical_channel,
        "logical_channel_index": mapping.logical_channel_index,
        "subchannel_name": mapping.subchannel_name,
        "subchannel_index": mapping.subchannel_index,
        "relative_shade": mapping.relative_shade,
        "head_offset": mapping.head_offset,
        "gutenprint_allocation_index": mapping.allocation_index,
        "model_channel_name_index": mapping.model_channel_name_index,
        "escp2_address": {
            "color": mapping.address.color,
            "subchannel": mapping.address.subchannel,
            "selector": f"0x{mapping.address.selector:02X}",
            "formula": "color | (subchannel << 4)",
        },
        "address_confidence": mapping.address_confidence.value,
        "expected_physical_position_confidence": mapping.expected_path_confidence.value,
        "physical_test_status": mapping.physical_test_status.value,
        "physically_observed_channel": None,
    }


def make_phase4_channel_tests(output_dir: Path) -> tuple[Path, ...]:
    """Generate, fully parse and compare all six file-only Phase 4 candidates."""

    jobs = tuple(generate_channel_test_job(channel) for channel in PhysicalChannel)
    validated = tuple(validate_channel_job(job) for job in jobs)
    phase3d_baseline = generate_phase3c_job()
    baseline_comparisons = {
        item.job.mapping.physical_channel: compare_to_phase3d_baseline(phase3d_baseline.data, item)
        for item in validated
    }
    comparisons = compare_channel_jobs(validated)

    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    profile = XP750_PHASE3C
    for item in validated:
        job = item.job
        channel = job.mapping.physical_channel
        stem = f"xp750_{channel.value}"
        prn_path = output_dir / f"{stem}.prn"
        manifest_path = output_dir / f"{stem}.json"
        analysis_path = output_dir / f"{stem}_analysis.txt"
        prn_path.write_bytes(job.data)
        manifest = {
            "phase": "4",
            "purpose": "Изоляционный тест одного физического пути подачи растра",
            "target_device": profile.target_device,
            "reference_model": profile.reference_model,
            "gutenprint_driver": profile.gutenprint_driver,
            "gutenprint_model": profile.gutenprint_model,
            "gutenprint_ink_group": "artisan",
            "ink_type": "PhotoCMYK / CcMmYK",
            "intended_physical_channel": channel.value,
            "source_derived_address": {
                "color": job.mapping.address.color,
                "subchannel": job.mapping.address.subchannel,
                "selector": f"0x{job.mapping.address.selector:02X}",
            },
            "confidence": {
                "address": job.mapping.address_confidence.value,
                "expected_physical_position": job.mapping.expected_path_confidence.value,
            },
            "evidence_reference": PHASE4_SOURCE_EVIDENCE,
            "resolution": "360x180 dpi",
            "media": profile.media,
            "input_slot": profile.input_slot,
            "print_method": "0x10",
            "raster_depth": profile.bit_depth,
            "compression": "TIFF PackBits (1)",
            "pass_count": len(job.passes),
            "raster_dimensions": [profile.raster_width_pixels, 360],
            "mapping": _phase4_mapping_dict(job.mapping),
            "source_evidence": PHASE4_SOURCE_EVIDENCE,
            "fixed_profile": {
                "resolution": "360x180 dpi",
                "media": profile.media,
                "input_slot": profile.input_slot,
                "color_configuration": 2,
                "print_method": "0x10",
                "bit_depth": profile.bit_depth,
                "compression": "TIFF PackBits (1)",
                "page": profile.page.name,
                "pass_count": len(job.passes),
                "rows_per_pass": [item.row_count for item in job.passes],
                "raster_width_pixels": profile.raster_width_pixels,
                "bytes_per_raster_row": profile.bytes_per_raster_row,
                "relative_vertical_advance": [item.vertical_advance for item in job.passes],
            },
            "pattern": job.geometry.as_dict(),
            "parser_validation": {
                "full_stream_consumed": True,
                "unknown_records": 0,
                "single_selector": f"0x{job.mapping.address.selector:02X}",
                "selector_offsets": [f"0x{offset:06X}" for offset in item.selector_offsets],
                "decoded_raster_sha256": list(item.decoded_raster_sha256),
                "packbits_round_trip": True,
                "second_bit_plane_empty": True,
                "phase3d_mechanics_unchanged": True,
                "phase3d_authorized_difference_offsets": [
                    f"0x{difference.offset:06X}"
                    for difference in baseline_comparisons[channel].differences
                ],
            },
            "prn_size": len(job.data),
            "file_size": len(job.data),
            "sha256": job.sha256,
            "physical_test_status": "NOT_TESTED",
            "implementation_version": __version__,
            "readiness": "READY_FOR_CONTROLLED_HARDWARE_TEST",
            "physical_verification": {
                "status": "NOT_TESTED",
                "observed_channel": None,
                "note": (
                    "Имя ожидаемого пути следует из локальных исходников Gutenprint; "
                    "физическое соответствие ещё не проверено изоляционной печатью."
                ),
            },
            "future_observation_categories": [
                "expected channel printed",
                "different channel printed",
                "multiple channels printed",
                "no print",
                "printer rejected job",
                "abnormal behavior",
            ],
        }
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        analysis_path.write_text(format_stream(list(item.records)), encoding="utf-8")
        written.extend((prn_path, manifest_path, analysis_path))

    mapping_path = output_dir / "channel_mapping.json"
    comparison_path = output_dir / "cross_channel_comparison.txt"
    master_manifest = {
        "phase": "4",
        "target_device": profile.target_device,
        "derivation": "Локальные исходники Gutenprint 5.3.5; перебор не применялся",
        "source_evidence": PHASE4_SOURCE_EVIDENCE,
        "source_mapping": [_phase4_mapping_dict(item) for item in XP750_SOURCE_CHANNELS],
        "physical_verification": {
            item.physical_channel.value: {
                "status": "NOT_TESTED",
                "observed_channel": None,
            }
            for item in XP750_SOURCE_CHANNELS
        },
        "phase3d_baseline": {
            "status": "SUCCESS",
            "date": "2026-08-12",
            "file": "output/phase3c_test.prn",
            "sha256": "bdc2939160a8a613fe48230e67454398b6f86f0c5ff92d86e30bc8f385290790",
            "scope": (
                "Подтверждены приём потока и геометрия; изолированное соответствие "
                "селекторов физическим путям не подтверждено."
            ),
        },
        "candidate_hashes": {
            item.job.mapping.physical_channel.value: item.job.sha256 for item in validated
        },
        "recommended_physical_test_order": [
            channel.value for channel in XP750_RECOMMENDED_TEST_ORDER
        ],
        "binary_comparison": {
            "pair_count": len(comparisons),
            "only_esc_i_selector_bytes_differ": True,
            "report": comparison_path.name,
        },
        "readiness": {
            item.job.mapping.physical_channel.value: "READY_FOR_CONTROLLED_HARDWARE_TEST"
            for item in validated
        },
    }
    mapping_path.write_text(
        json.dumps(master_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    comparison_path.write_text(format_channel_comparisons(comparisons), encoding="utf-8")
    written.extend((mapping_path, comparison_path))
    return tuple(written)


def _phase5_manifest(validated: ValidatedMultipathJob) -> dict[str, object]:
    profile = XP750_PHASE3C
    job = validated.job
    pattern_by_path = dict(job.patterns)
    plane_by_path = {plane.identity: plane for plane in job.rasters.planes}
    path_entries: list[dict[str, object]] = []
    for path in job.active_paths:
        descriptor = XP750_PHYSICAL_PATH_BY_ID[path]
        pattern = pattern_by_path[path]
        path_entries.append(
            {
                "physical_path": path.value,
                "factory_role": descriptor.factory_role.value,
                "selector": f"0x{descriptor.address.selector:02X}",
                "address": {
                    "color": descriptor.address.color,
                    "subchannel": descriptor.address.subchannel,
                },
                "gutenprint_allocation_index": descriptor.gutenprint_allocation_index,
                "pattern": {
                    **pattern.as_dict(),
                    "description_ru": _PHASE5_PATTERN_RUSSIAN[pattern.kind],
                },
                "active_bits": list(plane_by_path[path].active_bits),
                "head_offset": {
                    "gutenprint_raw_base_separation_units": (descriptor.gutenprint_head_offset),
                    "effective_raster_rows": descriptor.effective_head_offset(
                        vertical_dpi=profile.vertical_dpi,
                        base_separation_dpi=profile.base_separation_dpi,
                    ),
                },
            }
        )

    pass_schedule: list[dict[str, object]] = []
    block_index = 0
    for pass_index, scheduled_pass in enumerate(job.passes):
        blocks: list[dict[str, object]] = []
        for source_block in scheduled_pass.blocks:
            validation = validated.blocks[block_index]
            blocks.append(
                {
                    "physical_path": source_block.path.value,
                    "selector": f"0x{validation.selector:02X}",
                    "rows": validation.rows,
                    "decoded_width_pixels": validation.width_pixels,
                    "active_bits": list(validation.active_bits),
                    "record_offset": f"0x{validation.record_offset:06X}",
                    "decoded_sha256": validation.decoded_sha256,
                }
            )
            block_index += 1
        pass_schedule.append(
            {
                "pass_index": pass_index,
                "vertical_advance": scheduled_pass.vertical_advance,
                "cumulative_vertical_position": validated.vertical_positions[pass_index],
                "flush_order": [block.path.value for block in scheduled_pass.blocks],
                "selector_sequence": [
                    f"0x{selector:02X}" for selector in scheduled_pass.selector_sequence
                ],
                "raster_blocks": blocks,
            }
        )

    return {
        "phase": "5",
        "purpose": "Одновременный независимо управляемый вывод нескольких PhysicalPaths",
        "printer_profile": {
            "target_device": profile.target_device,
            "reference_model": profile.reference_model,
            "gutenprint_driver": profile.gutenprint_driver,
            "gutenprint_model": profile.gutenprint_model,
            "gutenprint_parameters": profile.gutenprint_parameters,
            "ink_type": "PhotoCMYK / CcMmYK",
        },
        "active_physical_paths": path_entries,
        "active_path_count": len(job.active_paths),
        "resolution": {
            "name": profile.resolution_name,
            "horizontal_dpi": profile.horizontal_dpi,
            "vertical_dpi": profile.vertical_dpi,
        },
        "media": profile.media,
        "input_slot": profile.input_slot,
        "print_method": f"0x{profile.print_method:02X}",
        "raster_dimensions": {
            "width_pixels": job.rasters.width_pixels,
            "height_logical_rows": job.rasters.height_rows,
            "bit_depth": job.rasters.bit_depth,
            "bytes_per_bit_plane_row": profile.bytes_per_plane_row,
            "bytes_per_esc_i_row": profile.bytes_per_raster_row,
        },
        "compression": {"id": profile.compression, "name": "TIFF PackBits"},
        "head_offset_derivation": {
            "formula": "effective = raw_headOffset * vertical_dpi / baseSeparation (integer)",
            "base_separation_dpi": profile.base_separation_dpi,
            "effective_offsets_all_zero": True,
            "classification": "FACT",
        },
        "pass_count": len(job.passes),
        "raster_block_count": validated.raster_block_count,
        "selector_sequence": [f"0x{selector:02X}" for selector in validated.selector_sequence],
        "vertical_advances": list(validated.vertical_advances),
        "vertical_positions": list(validated.vertical_positions),
        "pass_schedule": pass_schedule,
        "parser_validation": {
            "full_stream_consumed": True,
            "unknown_records": 0,
            "packbits_round_trip_all_blocks": True,
            "exact_active_path_set": True,
            "unexpected_selectors": [],
            "independent_raster_data": True,
            "page_job_termination": True,
        },
        "prn_size": len(job.data),
        "sha256": job.sha256,
        "source_evidence": PHASE5_SOURCE_EVIDENCE,
        "hardware_test_status": "PHYSICALLY_CONFIRMED",
        "readiness": "PHYSICALLY_VERIFIED",
        "physical_verification": {
            "status": "PHYSICALLY_CONFIRMED",
            "result": "SUCCESS",
            "evidence_class": "USER_REPORTED_PHYSICAL_TEST",
            "tested_physical_path_count": len(job.active_paths),
            "printer_accepted_prn": True,
            "all_expected_marks_printed": True,
            "all_expected_physical_paths_participated": True,
            "unexpected_physical_path_printed": False,
            "channel_data_mixing_observed": False,
            "visible_abnormal_interchannel_displacement_observed": False,
            "page_completed_normally": True,
            "printer_error_or_abnormal_behavior_observed": False,
            "test_date": None,
            "transport": None,
            "unknown_fields": [
                "UNKNOWN: дата физического теста не сообщена.",
                "UNKNOWN: способ raw-передачи PRN не сообщён.",
            ],
        },
        "implementation_version": __version__,
        "remaining_unknown": [
            "UNKNOWN: количественная точность межканального совмещения не измерена.",
            "UNKNOWN: поведение вне 360x180sw, Plain, LowerFront и Letter не установлено.",
            "UNKNOWN: минимально обязательное подмножество REMOTE1 отдельно не исследовано.",
        ],
    }


def _phase5_test_plan(validated_jobs: tuple[ValidatedMultipathJob, ...]) -> dict[str, object]:
    candidates: list[dict[str, object]] = []
    for validated in validated_jobs:
        expected_marks: list[dict[str, object]] = []
        pattern_by_path = dict(validated.job.patterns)
        for path in validated.job.active_paths:
            descriptor = XP750_PHYSICAL_PATH_BY_ID[path]
            pattern = pattern_by_path[path]
            expected_marks.append(
                {
                    "physical_path": path.value,
                    "factory_ink_color": descriptor.factory_role.value,
                    "selector": f"0x{descriptor.address.selector:02X}",
                    "mark": _PHASE5_PATTERN_RUSSIAN[pattern.kind],
                    "cell_bounds": list(pattern.bounds),
                    "active_bounds": list(pattern.active_bounds),
                }
            )
        count = len(validated.job.active_paths)
        candidates.append(
            {
                "order": count - 1,
                "path_count": count,
                "file": f"xp750_{count}path.prn",
                "sha256": validated.job.sha256,
                "expected_marks": expected_marks,
                "expected_geometry": (
                    "Все метки находятся в общей полосе y=150..209 logical rows; "
                    "каждая форма должна находиться только в собственном x-диапазоне."
                ),
                "pass_criteria": (
                    "Напечатаны все и только ожидаемые формы, каждая ожидаемым цветом "
                    "factory inkset, без заметного межканального сдвига, ошибки или аномалии."
                ),
                "stop_criteria": (
                    "Отсутствующая/лишняя/неверная форма или цвет, заметный сдвиг, смешение, "
                    "отказ job, ошибка принтера либо любое аномальное поведение."
                ),
                "hardware_test_status": "PHYSICALLY_CONFIRMED",
                "readiness": "PHYSICALLY_VERIFIED",
                "physical_result": {
                    "result": "SUCCESS",
                    "all_expected_marks_printed": True,
                    "all_expected_physical_paths_participated": True,
                    "unexpected_physical_path_printed": False,
                    "channel_data_mixing_observed": False,
                    "visible_abnormal_interchannel_displacement_observed": False,
                    "page_completed_normally": True,
                    "printer_error_or_abnormal_behavior_observed": False,
                },
            }
        )
    return {
        "phase": "5",
        "target_device": "Epson XP-750",
        "artifact_generator_printer_access_performed": False,
        "artifact_generator_network_access_performed": False,
        "physical_test_status": "PHYSICALLY_CONFIRMED",
        "physical_test_evidence_class": "USER_REPORTED_PHYSICAL_TEST",
        "physical_test_date": None,
        "physical_test_transport": None,
        "manual_test_order": [2, 3, 4, 5, 6],
        "global_gate": (
            "Передавать только один файл за раз. Любой результат STOP запрещает передачу "
            "следующего кандидата до анализа причины."
        ),
        "preflight": [
            "Сверить имя файла, размер и SHA-256 с manifest.",
            "Использовать Letter, Plain и LowerFront согласно фиксированному профилю.",
            "Убедиться, что принтер Ready и в нём установлен текущий factory inkset.",
            "Не применять драйверную переработку: передавать готовый PRN как raw job.",
        ],
        "candidates": candidates,
        "source_evidence": PHASE5_SOURCE_EVIDENCE,
    }


def make_phase5_multipath_tests(output_dir: Path) -> tuple[Path, ...]:
    """Generate and fully validate the five file-only Phase 5 candidates."""

    jobs = tuple(generate_xp750_multipath_test_job(count) for count in range(2, 7))
    validated_jobs = tuple(validate_multipath_job(job) for job in jobs)
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for validated in validated_jobs:
        count = len(validated.job.active_paths)
        stem = f"xp750_{count}path"
        prn_path = output_dir / f"{stem}.prn"
        manifest_path = output_dir / f"{stem}.json"
        analysis_path = output_dir / f"{stem}_analysis.txt"
        prn_path.write_bytes(validated.job.data)
        manifest_path.write_text(
            json.dumps(_phase5_manifest(validated), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        analysis_path.write_text(format_multipath_analysis(validated), encoding="utf-8")
        written.extend((prn_path, manifest_path, analysis_path))

    plan_path = output_dir / "multipath_test_plan.json"
    plan_path.write_text(
        json.dumps(_phase5_test_plan(validated_jobs), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    written.append(plan_path)
    return tuple(written)


def add_dev_commands(subparsers: argparse._SubParsersAction) -> None:
    make = subparsers.add_parser("make-test-prn", help="создать артефакты-кандидаты Phase 2")
    make.add_argument("--output-dir", type=Path, default=Path("output"))
    make.set_defaults(dev_handler=_make_test_prn)
    phase3c = subparsers.add_parser(
        "make-phase3c-prn", help="создать только файловые артефакты Phase 3C"
    )
    phase3c.add_argument("--output-dir", type=Path, default=Path("output"))
    phase3c.add_argument("--golden-reference", type=Path, default=DEFAULT_GOLDEN_REFERENCE)
    phase3c.set_defaults(dev_handler=_make_phase3c_prn)
    phase4 = subparsers.add_parser(
        "make-phase4-channel-tests", help="создать файловые артефакты Phase 4"
    )
    phase4.add_argument("--output-dir", type=Path, default=Path("output/channel_tests"))
    phase4.set_defaults(dev_handler=_make_phase4_channel_tests)
    phase5 = subparsers.add_parser(
        "make-phase5-multipath-tests", help="создать файловые артефакты Phase 5"
    )
    phase5.add_argument("--output-dir", type=Path, default=Path("output/multipath_tests"))
    phase5.set_defaults(dev_handler=_make_phase5_multipath_tests)
    qtr = subparsers.add_parser(
        "analyze-qtr-profile", help="создать файловые diagnostics готового QTR profile"
    )
    qtr.add_argument("quad", type=Path)
    qtr.add_argument("qidf", type=Path)
    qtr.add_argument("--output-dir", type=Path, default=Path("output/qtr_analysis"))
    qtr.set_defaults(dev_handler=_analyze_qtr_profile)
    phase7 = subparsers.add_parser(
        "make-phase7-first-image", help="создать file-only TIFF/QTR one-path PRN Phase 7"
    )
    phase7.add_argument(
        "--output-dir", type=Path, default=Path("output/phase7_first_image")
    )
    phase7.set_defaults(dev_handler=_make_phase7_first_image)
    phase8 = subparsers.add_parser(
        "make-phase8-artifacts", help="создать file-only Phase 8 test sets без передачи"
    )
    phase8.add_argument("--output-dir", type=Path, default=Path("output"))
    phase8.set_defaults(dev_handler=_make_phase8_artifacts)
    phase9 = subparsers.add_parser(
        "make-phase9-artifacts", help="создать offline mode/media candidates Phase 9"
    )
    phase9.add_argument("--output-dir", type=Path, default=Path("output"))
    phase9.set_defaults(dev_handler=_make_phase9_artifacts)
    inspect = subparsers.add_parser("inspect", help="разобрать созданный Phase 2 PRN")
    inspect.add_argument("path", type=Path)
    inspect.set_defaults(dev_handler=_inspect)


def _print_paths(paths: tuple[Path, ...]) -> None:
    print("\n".join(str(path) for path in paths))


def _make_test_prn(args: argparse.Namespace) -> None:
    _print_paths(make_test_prn(args.output_dir))


def _make_phase3c_prn(args: argparse.Namespace) -> None:
    _print_paths(make_phase3c_prn(args.output_dir, args.golden_reference))


def _make_phase4_channel_tests(args: argparse.Namespace) -> None:
    _print_paths(make_phase4_channel_tests(args.output_dir))


def _make_phase5_multipath_tests(args: argparse.Namespace) -> None:
    _print_paths(make_phase5_multipath_tests(args.output_dir))


def _analyze_qtr_profile(args: argparse.Namespace) -> None:
    _print_paths(generate_qtr_analysis(args.quad, args.qidf, args.output_dir))


def _make_phase7_first_image(args: argparse.Namespace) -> None:
    _print_paths(make_phase7_first_image(args.output_dir).paths)


def _make_phase8_artifacts(args: argparse.Namespace) -> None:
    from xp750_rip.phase8_artifacts import generate_phase8_artifacts

    _print_paths(generate_phase8_artifacts(args.output_dir).paths)


def _make_phase9_artifacts(args: argparse.Namespace) -> None:
    from xp750_rip.phase9_artifacts import generate_phase9_artifacts

    _print_paths(generate_phase9_artifacts(args.output_dir).paths)


def _inspect(args: argparse.Namespace) -> None:
    print(format_analysis(parse(args.path.read_bytes())), end="")


def dispatch_dev(args: argparse.Namespace) -> bool:
    handler = getattr(args, "dev_handler", None)
    if handler is None:
        return False
    handler(args)
    return True
