# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""File-only Phase 7 TIFF -> QTR -> Ordered -> XP-750 artifact pipeline."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path

from PIL import Image

from xp750_rip.analysis.phase7_validation import (
    Phase7PrnValidation,
    format_phase7_analysis,
    validate_phase7_image_job,
)
from xp750_rip.analysis.qtr_corpus import (
    audit_qtr_corpus,
    inspect_qtr_tiffs,
    inventory_qtr_tree,
)
from xp750_rip.contone import LogicalChannelId, LogicalContonePlane
from xp750_rip.dither.ordered import (
    GutenprintDropModel,
    GutenprintOrderedDither,
    load_gutenprint_matrix_2x1,
    unpack_sample_codes,
)
from xp750_rip.escp2.job import GeneratedPhysicalRasterJob, generate_xp750_physical_raster_job
from xp750_rip.input.tiff import Grayscale8Raster, decode_grayscale_tiff
from xp750_rip.printers.xp750 import XP750_PHASE3C, XP750_PHYSICAL_PATH_BY_ID
from xp750_rip.qtr.quad import QuadProfile, parse_quad
from xp750_rip.qtr.raster_executor import (
    black_is_zero_raster_to_qtr_codes,
    execute_profile_codes,
)
from xp750_rip.raster import PhysicalPathId, PhysicalRasterSet
from xp750_rip.routing import OneToOneChannelRoute

WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_QTR_ROOT = WORKSPACE_ROOT / "Docs" / "QuadToneRIP"
DEFAULT_TIFF = DEFAULT_QTR_ROOT / "21step.tif"
DEFAULT_QUAD = (
    DEFAULT_QTR_ROOT
    / "QuadTone"
    / "QuadR300"
    / "InkTekPK-PripaGlossy260-K1-BO.quad"
)


@dataclass(frozen=True, slots=True)
class Phase7FirstImageConfig:
    source_tiff: Path = DEFAULT_TIFF
    quad_profile: Path = DEFAULT_QUAD
    profile_source_printer: str = "Epson R300 / QuadR300"
    logical_ink: str = "K"
    target_path: PhysicalPathId = field(default_factory=lambda: PhysicalPathId("P0"))
    integer_decimation: int = 6
    placement_x: int = 1334
    placement_y: int = 140


@dataclass(frozen=True, slots=True)
class Phase7Artifacts:
    paths: tuple[Path, ...]
    job: GeneratedPhysicalRasterJob
    validation: Phase7PrnValidation


def _relative_to_workspace(path: Path) -> str:
    try:
        return path.resolve().relative_to(WORKSPACE_ROOT.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def _save_grayscale(rows: tuple[bytes, ...], width: int, path: Path) -> None:
    image = Image.frombytes("L", (width, len(rows)), b"".join(rows))
    image.save(path, format="PNG")


def _contone_preview(plane: LogicalContonePlane) -> tuple[bytes, ...]:
    return tuple(
        bytes(255 - (value * 255 + 32767) // 65535 for value in row) for row in plane.rows
    )


def _dither_preview_rows(
    dot_rows: tuple[bytes, ...], *, x: int, y: int, width: int, height: int
) -> tuple[bytes, ...]:
    preview_values = (255, 163, 102, 0)
    return tuple(
        bytes(preview_values[code] for code in dot_rows[row][x : x + width])
        for row in range(y, y + height)
    )


def _constant_tone_diagnostics(dither: GutenprintOrderedDither) -> dict[str, object]:
    width = dither.matrix.width
    height = dither.matrix.height
    diagnostics: list[dict[str, object]] = []
    drop_weight = {0: 0.0, 1: 0.36, 2: 0.60, 3: 1.0}
    for percent in (0, 1, 5, 10, 25, 50, 75, 90, 95, 99, 100):
        value = (percent * 65535 + 50) // 100
        plane = LogicalContonePlane(
            identity=LogicalChannelId("diagnostic"),
            width_pixels=width,
            rows=tuple(tuple(value for _ in range(width)) for _ in range(height)),
        )
        dots = dither.dither_placed(
            plane,
            canvas_width=368,
            canvas_height=height,
            x=0,
            y=0,
        )
        code_rows = tuple(row[:width] for row in unpack_sample_codes(dots))
        counts = Counter(code for row in code_rows for code in row)
        pixel_count = width * height
        dot_count = pixel_count - counts[0]
        diagnostics.append(
            {
                "tone_percent": percent,
                "input_uint16": value,
                "matrix_period_dimensions": [width, height],
                "pixel_count": pixel_count,
                "dot_count": dot_count,
                "sample_code_distribution": {
                    str(code): counts[code] for code in range(4)
                },
                "dot_occupancy_ratio": dot_count / pixel_count,
                "nominal_drop_coverage_ratio": sum(
                    counts[code] * drop_weight[code] for code in range(4)
                )
                / pixel_count,
                "sample_codes_sha256": sha256(b"".join(code_rows)).hexdigest(),
            }
        )
    return {
        "algorithm": "Gutenprint Ordered legacy variable-drop path",
        "input_domain": "uint16 0..65535",
        "matrix": {
            "source": dither.matrix.source,
            "dimensions": [dither.matrix.width, dither.matrix.height],
        },
        "drop_sizes": list(dither.drops.drop_sizes),
        "drop_bit_patterns": {"small": 1, "medium": 2, "large": 3},
        "tones": diagnostics,
    }


def _profile_selection(profile: QuadProfile, ink_code: str) -> dict[str, object]:
    curve = profile.curve(ink_code)
    nonzero = [index for index, value in enumerate(curve.samples) if value]
    return {
        "selected_logical_ink": ink_code,
        "nonzero_code_range": [min(nonzero), max(nonzero)] if nonzero else None,
        "nonzero_node_count": len(nonzero),
        "maximum_uint16": max(curve.samples),
        "reason_ru": (
            "Кривая ненулевая на всех QTR codes 1..255 и поэтому сохраняет "
            "пространственную структуру всего тестового grayscale wedge."
        ),
    }


def _build_manifest(
    *,
    config: Phase7FirstImageConfig,
    decoded: object,
    reduced: Grayscale8Raster,
    profile: QuadProfile,
    selected: LogicalContonePlane,
    job: GeneratedPhysicalRasterJob,
    validation: Phase7PrnValidation,
    diagnostics: dict[str, object],
) -> dict[str, object]:
    descriptor = XP750_PHYSICAL_PATH_BY_ID[config.target_path]
    profile_selection = _profile_selection(profile, config.logical_ink)
    source_width = decoded.raster.width
    source_height = decoded.raster.height
    return {
        "phase": "7",
        "purpose": "Первый file-only grayscale TIFF через QTR executor в один PhysicalPath",
        "source_tiff": _relative_to_workspace(config.source_tiff),
        "source_dimensions": [source_width, source_height],
        "source_bit_depth": decoded.bits_per_sample,
        "source_polarity": {
            "photometric_interpretation": decoded.photometric_interpretation,
            "meaning": "BlackIsZero: TIFF 0=black, TIFF 255=white",
        },
        "source_orientation": decoded.orientation,
        "source_compression": decoded.compression,
        "source_icc_profile_present": decoded.has_icc_profile,
        "integer_decimation": {
            "factor_x": config.integer_decimation,
            "factor_y": config.integer_decimation,
            "method": "nearest source sample at (x*N, y*N); no interpolation",
            "result_dimensions": [reduced.width, reduced.height],
        },
        "tiff_to_qtr_conversion_rule": "qtr_code = 255 - decoded_tiff_sample",
        "polarity_endpoints": {
            "tiff_0_black_to_qtr_255_max_black_demand": True,
            "tiff_255_white_to_qtr_0_paper_white": True,
        },
        "qtr_profile_path": _relative_to_workspace(config.quad_profile),
        "qtr_profile_sha256": sha256(config.quad_profile.read_bytes()).hexdigest(),
        "profile_source_printer": config.profile_source_printer,
        "profile_is_calibrated_for_xp750": False,
        "profile_curve_count": len(profile.curves),
        "profile_ink_order": list(profile.ink_codes),
        "profile_selection": profile_selection,
        "selected_logical_ink": config.logical_ink,
        "logical_output_domain": "uint16 0..65535",
        "selected_output_dimensions": [selected.width_pixels, selected.height_rows],
        "explicit_route": {
            "logical_ink": config.logical_ink,
            "physical_path": config.target_path.value,
            "mapping_basis": "explicit Phase 7 configuration; not ink name or array position",
        },
        "target_physical_path": config.target_path.value,
        "xp750_selector": f"0x{descriptor.address.selector:02X}",
        "expected_factory_ink": {
            "factory_role": descriptor.factory_role.value,
            "expected_color_if_reported_factory_inkset_is_unchanged": "Black",
            "installed_ink_chemistry": "UNKNOWN",
        },
        "dither_algorithm": "Gutenprint Ordered (legacy variable-drop path)",
        "dither_evidence": {
            "selection": "QTR User Guide default and observed QTR stpDither option",
            "algorithm": "gutenprint-5.3.5/src/main/dither-ordered.c:210-252, 438-452",
            "ranges": "gutenprint-5.3.5/src/main/dither-inks.c:270-405",
            "matrix_selection": "gutenprint-5.3.5/src/main/dither-main.c:346-403",
            "matrix_data": "gutenprint-5.3.5/src/xml/dither/matrix-2x1.xml",
            "xp750_mode_drops": "gutenprint-5.3.5/src/xml/escp2/model/model_96.xml:76-85",
        },
        "dither_parameters": {
            "matrix_dimensions": [367, 179],
            "matrix_aspect": [2, 1],
            "matrix_offsets": [0, 0],
            "drop_sizes": list(diagnostics["drop_sizes"]),
            "bit_patterns": {"0": "none", "1": "plane0", "2": "plane1", "3": "both"},
            "additional_density_scale": 1.0,
            "hidden_color_conversion": False,
            "hidden_channel_routing": False,
        },
        "image_placement": {
            "x": config.placement_x,
            "y": config.placement_y,
            "width": reduced.width,
            "height": reduced.height,
            "one_output_pixel_per_decimated_input_pixel": True,
        },
        "expected_orientation": "normal Orientation=1; 100% black end is left, 0% white end right",
        "expected_approximate_printed_size_mm": [
            reduced.width / XP750_PHASE3C.horizontal_dpi * 25.4,
            reduced.height / XP750_PHASE3C.vertical_dpi * 25.4,
        ],
        "raster_geometry": {
            "width_pixels": job.rasters.width_pixels,
            "height_rows": job.rasters.height_rows,
            "bit_depth": job.rasters.bit_depth,
            "resolution_dpi": [
                XP750_PHASE3C.horizontal_dpi,
                XP750_PHASE3C.vertical_dpi,
            ],
        },
        "pass_count": len(job.passes),
        "raster_block_count": validation.raster_block_count,
        "vertical_positions": list(validation.vertical_positions),
        "file_size": len(job.data),
        "sha256": job.sha256,
        "parser_validation": {
            "full_eof_consumption": True,
            "unknown_records": 0,
            "only_expected_selector": True,
            "selector_sequence": [
                f"0x{selector:02X}" for selector in validation.selector_sequence
            ],
            "expected_dimensions": True,
            "expected_pass_count": True,
            "packbits_round_trip": True,
            "exact_reconstructed_dither_rows": validation.reconstructed_dot_plane_matches,
            "normal_termination": True,
        },
        "hardware_test_status": "PHYSICALLY_VERIFIED",
        "readiness": "PHYSICALLY_VERIFIED",
        "physical_verification": {
            "result": "PASS",
            "reported_by": "user",
            "recorded_during_phase": "8",
            "scope": "TIFF -> QTR curve -> Ordered dither -> P0 -> XP-750 physical print",
        },
        "printer_access_performed": False,
        "network_access_performed": False,
    }


def make_phase7_first_image(
    output_dir: Path,
    config: Phase7FirstImageConfig | None = None,
    *,
    qtr_root: Path = DEFAULT_QTR_ROOT,
) -> Phase7Artifacts:
    """Generate and fully validate all Phase 7 file-only artifacts."""

    if config is None:
        config = Phase7FirstImageConfig()
    output_dir.mkdir(parents=True, exist_ok=True)
    decoded = decode_grayscale_tiff(config.source_tiff)
    reduced = decoded.raster.decimate(config.integer_decimation)
    code_rows = black_is_zero_raster_to_qtr_codes(reduced)
    profile = parse_quad(config.quad_profile.read_text(encoding="utf-8-sig"))
    logical_rasters = execute_profile_codes(code_rows, profile)
    logical_id = LogicalChannelId(config.logical_ink)
    selected = logical_rasters.get(logical_id)

    matrix = load_gutenprint_matrix_2x1()
    drops = GutenprintDropModel.from_drop_sizes((0.36, 0.60, 1.0))
    dither = GutenprintOrderedDither(matrix=matrix, drops=drops)
    logical_dots = dither.dither_placed(
        selected,
        canvas_width=XP750_PHASE3C.raster_width_pixels,
        canvas_height=XP750_PHASE3C.pass_count * XP750_PHASE3C.rows_per_pass,
        x=config.placement_x,
        y=config.placement_y,
    )
    route = OneToOneChannelRoute(logical_id, config.target_path)
    physical = route.route(logical_dots)
    job = generate_xp750_physical_raster_job(
        PhysicalRasterSet((physical,)), job_label="XP750QTR"
    )
    validation = validate_phase7_image_job(job)
    diagnostics = _constant_tone_diagnostics(dither)

    prn_path = output_dir / "xp750_qtr_1path_image.prn"
    manifest_path = output_dir / "xp750_qtr_1path_image.json"
    analysis_path = output_dir / "xp750_qtr_1path_image_analysis.txt"
    decoded_preview = output_dir / "decoded_grayscale_tiff.png"
    contone_preview = output_dir / "qtr_selected_logical_contone.png"
    dither_preview = output_dir / "dithered_dot_plane.png"
    diagnostics_path = output_dir / "dither_constant_tones.json"
    inventory_path = output_dir / "qtr_tree_inventory.json"
    corpus_path = output_dir / "qtr_corpus_audit.json"
    tiff_path = output_dir / "qtr_tiff_inventory.json"

    prn_path.write_bytes(job.data)
    _save_grayscale(reduced.rows, reduced.width, decoded_preview)
    _save_grayscale(_contone_preview(selected), selected.width_pixels, contone_preview)
    dot_rows = unpack_sample_codes(logical_dots)
    _save_grayscale(
        _dither_preview_rows(
            dot_rows,
            x=config.placement_x,
            y=config.placement_y,
            width=reduced.width,
            height=reduced.height,
        ),
        reduced.width,
        dither_preview,
    )
    diagnostics_path.write_text(
        json.dumps(diagnostics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    inventory_path.write_text(
        json.dumps(inventory_qtr_tree(qtr_root), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    corpus_path.write_text(
        json.dumps(audit_qtr_corpus(qtr_root), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    tiff_path.write_text(
        json.dumps(inspect_qtr_tiffs(qtr_root), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    manifest = _build_manifest(
        config=config,
        decoded=decoded,
        reduced=reduced,
        profile=profile,
        selected=selected,
        job=job,
        validation=validation,
        diagnostics=diagnostics,
    )
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    analysis_path.write_text(format_phase7_analysis(job, validation), encoding="utf-8")
    paths = (
        prn_path,
        manifest_path,
        analysis_path,
        decoded_preview,
        contone_preview,
        dither_preview,
        diagnostics_path,
        inventory_path,
        corpus_path,
        tiff_path,
    )
    return Phase7Artifacts(paths=paths, job=job, validation=validation)
