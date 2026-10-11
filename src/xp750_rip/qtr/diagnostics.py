# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Machine-readable Phase 6 diagnostics for a compiled QTR profile."""

from __future__ import annotations

import csv
import json
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

from xp750_rip.qtr.executor import evaluate_profile_code
from xp750_rip.qtr.qidf import QidfDocument, QidfPartition, parse_qidf
from xp750_rip.qtr.quad import QTR_UINT16_MAX, QuadProfile, parse_quad

PHASE6_SAMPLE_CODES = (0, 1, 16, 32, 64, 96, 128, 160, 192, 224, 240, 254, 255)

FORMAT_SEMANTICS_GENERIC = "FORMAT/SEMANTICS-GENERIC"
R300_SPECIFIC = "R300-SPECIFIC"
XP750_VERIFIED = "XP750-VERIFIED"
UNKNOWN = "UNKNOWN"


def _decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _partition_dict(partition: QidfPartition) -> dict[str, object]:
    return {
        "name": partition.name,
        "declared_part_count_legacy": partition.declared_part_count,
        "parts": [
            {
                "ordinal": part.ordinal,
                "ink_code": part.ink_code,
                "relative_density_percent": str(part.relative_density),
            }
            for part in partition.parts
        ],
        "highlight_raw": _decimal_text(partition.highlight),
        "shadow_raw": _decimal_text(partition.shadow),
        "gamma_raw": _decimal_text(partition.gamma),
        "overlap_raw": _decimal_text(partition.overlap),
        "correction_raw": partition.correction_raw,
    }


def _qidf_dict(document: QidfDocument, source_path: Path) -> dict[str, object]:
    return {
        "source_path": str(source_path),
        "source_sha256": sha256(source_path.read_bytes()).hexdigest(),
        "evidence_scope": {
            "descriptor_format": FORMAT_SEMANTICS_GENERIC,
            "supplied_field_values": R300_SPECIFIC,
            "printer_codename": R300_SPECIFIC,
            "xp750_backend_parameters_imported": False,
        },
        "platform_extension_fact": (
            "Windows uses .qidf; macOS uses .txt for the same QIDF format "
            "(Quad Ink Descriptor specification, page 5)."
        ),
        "printer_codename": document.printer,
        "curve_name_legacy": document.curve_name,
        "default_ink_limit_percent": _decimal_text(document.default_ink_limit),
        "boost_k_percent": _decimal_text(document.boost_k),
        "limits_percent": {ink: _decimal_text(value) for ink, value in document.limits.items()},
        "copy_curves": document.copy_curves,
        "linearize_measurement_values": [str(value) for value in (document.linearize_values or ())],
        "partitions": {
            name: _partition_dict(document.partition(name)) for name in ("GRAY", "TONER", "TONER_2")
        },
        "unknown_keys": list(document.unknown_keys),
        "preserved_entries": [
            {
                "line_number": entry.line_number,
                "kind": entry.kind,
                "original_key": entry.original_key,
                "normalized_key": entry.normalized_key,
                "raw_value": entry.raw_value,
                "raw_line": entry.raw,
            }
            for entry in document.entries
        ],
    }


def _profile_dict(
    profile: QuadProfile,
    source_path: Path,
    descriptor: QidfDocument,
    descriptor_path: Path,
) -> dict[str, object]:
    return {
        "schema": "xp750-rip-qtr-analysis-v1",
        "phase": 6,
        "source_path": str(source_path),
        "source_sha256": sha256(source_path.read_bytes()).hexdigest(),
        "evidence_scope": {
            "quad_syntax_and_table_semantics": FORMAT_SEMANTICS_GENERIC,
            "supplied_curve_values_and_ink_codes": R300_SPECIFIC,
            "xp750_backend": XP750_VERIFIED,
            "undocumented_runtime_behavior": UNKNOWN,
            "automatic_r300_ink_to_xp750_path_mapping": False,
            "r300_backend_parameters_imported": [],
        },
        "quadprofile_version_comment": profile.quadprofile_version,
        "declared_inks_comment": list(profile.declared_inks or ()),
        "curve_count": len(profile.curves),
        "input_domain": {
            "kind": "exact 8-bit QTR tonal-demand table index",
            "integer_range": [0, 255],
            "normalized_mapping": "input_code / 255",
            "polarity": "0=paper white/no ink demand; 255=maximum image-black demand",
            "off_grid_interpolation": "UNKNOWN_AND_REJECTED",
        },
        "output_domain": {
            "kind": "compiled QTR unsigned 16-bit logical ink amount",
            "integer_range": [0, QTR_UINT16_MAX],
            "normalized_mapping": "raw_uint16 / 65535",
            "zero_semantics": "no output for that logical ink",
            "physical_ink_load_semantics": "NOT_ESTABLISHED_FOR_SUM_OF_CHANNELS",
        },
        "execution_semantics": {
            "evidence_scope": FORMAT_SEMANTICS_GENERIC,
            "operation": "direct table lookup for every logical ink at one 8-bit index",
            "profile_authoring_operations": (
                "Already compiled into .quad by quadprofile/MakeQuad; LIMIT, partition, "
                "crossover, overlap and LINEARIZE are not reapplied."
            ),
            "output_binding": "logical ink codes only; no PhysicalPath or printer selector",
        },
        "curves": [
            {
                "logical_ink_code": curve.ink_code,
                "sample_values_scope": R300_SPECIFIC,
                "source_order": index,
                "marker_line_number": curve.marker_line_number,
                "sample_count": len(curve.samples),
                "minimum": min(curve.samples),
                "maximum": max(curve.samples),
                "first_nonzero_index": next(
                    (i for i, value in enumerate(curve.samples) if value), None
                ),
                "last_nonzero_index": next(
                    (i for i in range(255, -1, -1) if curve.samples[i]), None
                ),
                "samples_raw_uint16": list(curve.samples),
            }
            for index, curve in enumerate(profile.curves)
        ],
        "descriptor_source": _qidf_dict(descriptor, descriptor_path),
        "preservation": {
            "exact_quad_text_round_trip": profile.serialize_preserved() == profile.source_text,
            "exact_qidf_text_round_trip": (
                descriptor.serialize_preserved() == descriptor.source_text
            ),
            "quad_source_line_count": len(profile.lines),
            "qidf_source_line_count": len(descriptor.entries),
        },
    }


def _write_curve_samples(path: Path, profile: QuadProfile) -> None:
    fieldnames = ["input_code", "input_tone"] + [f"{ink}_raw_uint16" for ink in profile.ink_codes]
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        for input_code in range(256):
            row: dict[str, object] = {
                "input_code": input_code,
                "input_tone": f"{input_code / 255:.12f}",
            }
            for curve in profile.curves:
                row[f"{curve.ink_code}_raw_uint16"] = curve.samples[input_code]
            writer.writerow(row)


def _write_execution_samples(path: Path, profile: QuadProfile) -> None:
    fieldnames = ["input_code", "input_tone"]
    for ink in profile.ink_codes:
        fieldnames.extend((f"{ink}_raw_uint16", f"{ink}_normalized"))
    fieldnames.extend(("active_inks", "total_raw_output_diagnostic_only"))
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        for input_code in PHASE6_SAMPLE_CODES:
            result = evaluate_profile_code(input_code, profile)
            row: dict[str, object] = {
                "input_code": input_code,
                "input_tone": f"{result.input_tone:.12f}",
                "active_inks": ",".join(result.active_inks),
                "total_raw_output_diagnostic_only": result.total_raw_output,
            }
            for amount in result.amounts:
                row[f"{amount.ink_code}_raw_uint16"] = amount.raw_uint16
                row[f"{amount.ink_code}_normalized"] = f"{amount.normalized:.12f}"
            writer.writerow(row)


def _runtime_pipeline_text() -> str:
    return """Phase 6: хронология R300 QTR runtime по Docs/log-quadtoterip.txt

R300-SPECIFIC — QTRgui 2.7.0.0 запускает ImageToRaster.exe 2.7.8.0.
Вход: E:\\Qtr-print\\my-test-foto-free-photo.tif.
Параметры: QuadR300.PPD, PRINTER=QuadR300, MediaType=Photo, PageSize=A6,
position=center, scale-percent=136, Landscape=yes, Resolution=2880,
ColorModel=QTRIP.
Ответственность: TIFF decoding, orientation/scaling/page placement и формирование
8-bit, 24-bpp CUPS raster (cupsColorSpace=1, width=2790, height=4020).

UNKNOWN edge — вероятная передача CUPS raster/stdout ImageToRaster на stdin
QuadToPrinter. Log показывает команды в этом порядке, но не печатает символ pipe
или имена временных raster-файлов.

R300-SPECIFIC — QTRgui запускает QuadToPrinter.exe 2.8.2.0.
Параметры: тот же PPD/printer/media/page/resolution; ripCurve1; blend controls;
stpDither=Ordered; ripGamma=0; ripLimit=0; ColorModel=QTRIP.
QuadToPrinter загружает готовый .quad, выбирает Gutenprint driver escp2-R300,
resolution 2880, media Photo, output type 4 и DitherAlgorithm=Ordered.
UNKNOWN — точные внутренние границы curve processing, Gutenprint dithering,
weave и Epson stream generation в закрытом QuadToPrinter, а также data contract
между этими стадиями.

UNKNOWN edge — вероятная передача printer byte stream/stdout QuadToPrinter на
stdin RawPrint. Аргумент '-' согласуется со stdin, но log не показывает pipe.

R300-SPECIFIC — QTRgui запускает RawPrint.exe 2.3.0.0 для очереди
"Epson Stylus Photo R300 (M) (Copy 1)" с аргументом '-'.
Ответственность: transport в Windows printer queue. Имя TIFF после '-' является
job/display name, а не доказанным вторым чтением TIFF.

FORMAT/SEMANTICS-GENERIC — разделение profile parsing/execution, logical ink
outputs, routing и printer backend является переносимой архитектурной границей.
R300-SPECIFIC — PPD, driver, ink codes/order, resolution, page/media, margins,
dot/weave parameters и queue из этого log не являются данными XP-750.
XP750-VERIFIED — XP-750 backend остаётся отдельным результатом Phase 3/4/5;
этот R300 log не добавляет к нему ни одного protocol parameter.

Граф:
TIFF -> ImageToRaster -> [CUPS raster, edge UNKNOWN] -> QuadToPrinter
-> [printer stream, edge UNKNOWN] -> RawPrint -> Windows queue/printer
"""


def generate_qtr_analysis(
    quad_path: Path,
    qidf_path: Path,
    output_dir: Path,
) -> tuple[Path, ...]:
    """Parse real inputs and write deterministic, file-only Phase 6 diagnostics."""

    profile = parse_quad(quad_path.read_text(encoding="utf-8-sig"))
    descriptor = parse_qidf(qidf_path.read_text(encoding="utf-8-sig"))
    output_dir.mkdir(parents=True, exist_ok=True)

    parsed_path = output_dir / "parsed_profile.json"
    curves_path = output_dir / "curve_samples.csv"
    samples_path = output_dir / "execution_samples.csv"
    pipeline_path = output_dir / "qtr_runtime_pipeline.txt"

    parsed_path.write_text(
        json.dumps(
            _profile_dict(profile, quad_path, descriptor, qidf_path),
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    _write_curve_samples(curves_path, profile)
    _write_execution_samples(samples_path, profile)
    pipeline_path.write_text(_runtime_pipeline_text(), encoding="utf-8")
    return parsed_path, curves_path, samples_path, pipeline_path
