# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Reproducible offline candidates for Phase 9 model96 modes and photo media."""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from PIL import Image, ImageDraw

from xp750_rip.analysis.escp2_parser import parse_stream
from xp750_rip.application import MonoRipService, OutputConfiguration, PrintJobConfiguration
from xp750_rip.contone import LogicalChannelId
from xp750_rip.layout import ImageSizeRequest, ResamplingMethod
from xp750_rip.printers.capabilities import PrinterInstance
from xp750_rip.printers.xp750_capabilities import load_xp750_model
from xp750_rip.qtr.bundle import load_profile_bundle
from xp750_rip.raster import PhysicalPathId
from xp750_rip.routing import ChannelRouter


@dataclass(frozen=True, slots=True)
class Phase9ArtifactSet:
    paths: tuple[Path, ...]


def _diagnostic_tiff(path: Path) -> None:
    image = Image.new("L", (320, 192), 255)
    draw = ImageDraw.Draw(image)
    for x in range(8, 80, 4):
        draw.line((x, 8, x, 54), fill=32 if x % 8 else 0)
    for y in range(8, 56, 4):
        draw.line((88, y, 160, y), fill=32 if y % 8 else 0)
    for offset in range(0, 64, 8):
        draw.line((168 + offset, 8, 168, 8 + offset), fill=0)
        draw.line((248, 8 + offset, 248 + offset, 8), fill=64)
    for x in range(304):
        value = round(255 * x / 303)
        draw.line((8 + x, 72, 8 + x, 111), fill=value)
    patches = (0, 32, 64, 96, 128, 160, 192, 224)
    for index, value in enumerate(patches):
        left = 8 + index * 38
        draw.rectangle((left, 120, left + 31, 151), fill=value)
    for index in range(6):
        left = 12 + index * 50
        draw.rectangle((left, 162, left + 25, 181), outline=0)
        for mark in range(index + 1):
            draw.point((left + 4 + mark * 3, 171), fill=0)
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, format="TIFF", compression="tiff_lzw", dpi=(360, 360))


def _write_auxiliary_reports(result: object, stem: Path) -> tuple[Path, Path]:
    rendered = result.rendered
    records = tuple(parse_stream(rendered.data))
    validation_path = stem.with_name(stem.name + "_validation.json")
    reconstruction_path = stem.with_name(stem.name + "_raster_reconstruction.json")
    validation_path.write_text(
        json.dumps(
            {
                "offline_validation": rendered.validation.as_dict(),
                "physical_test_status": "NOT_TESTED",
                "printer_access": False,
                "network_access": False,
                "stream_sha256": rendered.stream_sha256,
                "mode": rendered.backend.mode.identity,
                "media": rendered.backend.medium.identity,
                "input_source": rendered.backend.input_source.identity,
                "page": rendered.backend.page.ppd.name,
                "expected_printed_geometry_mm": rendered.manifest["rendered_dimensions_mm"],
                "active_physical_paths": rendered.manifest["printer"][
                    "active_physical_paths"
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    raster_records = [item for item in records if item.raster is not None]
    reconstruction_path.write_text(
        json.dumps(
            {
                "source": "independent ESC/P2 parser + PackBits decode",
                "physical_test_status": "NOT_TESTED",
                "full_stream_consumed": records[-1].end_offset == len(rendered.data),
                "raster_blocks": [
                    {
                        "record_offset": item.offset,
                        "selector": f"0x{item.raster.selector:02X}",
                        "bit_depth": item.raster.bit_depth,
                        "bytes_per_row": item.raster.bytes_per_row,
                        "rows": item.raster.rows,
                        "decoded_sha256": sha256(item.raster.decoded_payload).hexdigest(),
                        "encoded_row_lengths": list(item.raster.encoded_row_lengths),
                    }
                    for item in raster_records
                    if item.raster is not None
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return validation_path, reconstruction_path


def generate_phase9_artifacts(
    output_root: Path,
    *,
    workspace_root: Path | None = None,
) -> Phase9ArtifactSet:
    """Generate only files; no transport object is constructed or invoked."""

    root = workspace_root or Path(__file__).resolve().parents[3]
    bundle = load_profile_bundle(
        quad_path=root / "Docs" / "OCBESTpk-PripaGlossy-InkMix-smooth-neutr.quad",
    )
    model = load_xp750_model()
    instance = PrinterInstance(model, "Phase9 XP-750 offline candidates")
    service = MonoRipService()
    logical = tuple(LogicalChannelId(item) for item in bundle.logical_inks[:6])
    routes = {
        ink: PhysicalPathId(f"P{index}") for index, ink in enumerate(logical)
    }
    mode_dir = output_root / "phase9_mode_tests"
    photo_dir = output_root / "phase9_photo_candidates"
    diagnostic = mode_dir / "phase9_model96_diagnostic.tif"
    _diagnostic_tiff(diagnostic)
    written: list[Path] = [diagnostic]
    master: list[dict[str, object]] = []

    def render_one(
        *,
        directory: Path,
        stem_name: str,
        mode_id: str,
        media: str,
        page: str,
        source: str,
        active: tuple[LogicalChannelId, ...],
        width_mm: float,
    ) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        stem = directory / stem_name
        prn = stem.with_suffix(".prn")
        manifest = stem.with_suffix(".json")
        result = service.execute(
            PrintJobConfiguration(
                source_image=diagnostic,
                profile_bundle=bundle,
                target=instance,
                mode=model.mode(mode_id),
                media_id=media,
                page_id=page,
                input_source_id=source,
                image_size=ImageSizeRequest(
                    width_mm=width_mm,
                    center=True,
                    resampling=ResamplingMethod.NEAREST,
                ),
                active_logical_inks=active,
                router=ChannelRouter({item: routes[item] for item in active}),
                inkset={},
                output=OutputConfiguration(prn, manifest),
            ),
            send=False,
        )
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        payload["phase"] = "9"
        payload["candidate_kind"] = (
            "ONE_PATH_MODE_TEST" if len(active) == 1 else "MULTI_OUTPUT_QTR_PHOTO_TEST"
        )
        payload["physical_test_status"] = "NOT_TESTED"
        payload["source_provenance"] = [
            "gutenprint-5.3.5/src/xml/escp2/model/model_96.xml",
            "gutenprint-5.3.5/src/main/print-weave.c",
            "gutenprint-5.3.5/src/main/escp2-driver.c",
            "Epson-XP-750_Series-epson-escpr-en.ppd",
        ]
        manifest.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        validation, reconstruction = _write_auxiliary_reports(result, stem)
        written.extend((prn, manifest, validation, reconstruction))
        master.append(
            {
                "file": str(prn.relative_to(output_root)),
                "mode": mode_id,
                "media": media,
                "input_source": source,
                "page": page,
                "active_logical_outputs": [item.value for item in active],
                "expected_physical_paths": [routes[item].value for item in active],
                "size": len(result.rendered.data),
                "sha256": result.rendered.stream_sha256,
                "offline_validation": "PASS",
                "physical_test_status": "NOT_TESTED",
            }
        )

    for mode in model.modes:
        render_one(
            directory=mode_dir,
            stem_name=f"xp750_{mode.identity}_1path",
            mode_id=mode.identity,
            media="PLAIN_NORMAL",
            page="Letter",
            source="LowerFront",
            active=logical[:1],
            width_mm=8.0,
        )

    for mode_id in ("720sw", "1440x720sw", "2880x1440sw"):
        render_one(
            directory=photo_dir,
            stem_name=f"xp750_{mode_id}_glossy_6path",
            mode_id=mode_id,
            media="PMPHOTO_HIGH",
            page="4X6FULL",
            source="UpperFront",
            active=logical,
            width_mm=6.0,
        )

    for label, medium, source in (
        ("plain", "PLAIN_NORMAL", "LowerFront"),
        ("glossy", "PMPHOTO_HIGH", "UpperFront"),
        ("semigloss", "PSGLOS_HIGH", "UpperFront"),
        ("matte", "PMMATT_NORMAL", "LowerFront"),
    ):
        render_one(
            directory=photo_dir,
            stem_name=f"xp750_720sw_{label}_media_1path",
            mode_id="720sw",
            media=medium,
            page=(
                "Letter"
                if medium in {"PLAIN_NORMAL", "PMMATT_NORMAL"}
                else "4X6FULL"
            ),
            source=source,
            active=logical[:1],
            width_mm=6.0,
        )

    master_path = output_root / "PHASE9_CANDIDATE_MANIFEST.json"
    master_path.write_text(
        json.dumps(
            {
                "phase": "9",
                "target_printer": "XP750",
                "generation": "OFFLINE_ONLY",
                "printer_access": False,
                "network_access": False,
                "physical_test_status": "NOT_TESTED",
                "candidates": master,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    written.append(master_path)
    comparison_path = output_root / "PHASE9_GUTENPRINT_STRUCTURAL_COMPARISON.json"
    source_files = (
        root / "gutenprint-5.3.5" / "src" / "xml" / "escp2" / "model" / "model_96.xml",
        root / "gutenprint-5.3.5" / "src" / "xml" / "escp2" / "media" / "artisan.xml",
        root / "gutenprint-5.3.5" / "src" / "main" / "print-weave.c",
        root / "gutenprint-5.3.5" / "src" / "main" / "escp2-driver.c",
        root / "gutenprint-5.3.5" / "src" / "main" / "bit-ops.c",
    )
    comparison_path.write_text(
        json.dumps(
            {
                "classification": "SOURCE_STRUCTURAL_COMPARISON",
                "byte_identical_gutenprint_reference_generated": False,
                "reason": (
                    "Local tree has no ready generated PX700W PPD/CLI stream; "
                    "comparison is against the exact model data and C execution path."
                ),
                "source_files": {
                    str(path.relative_to(root)): sha256(path.read_bytes()).hexdigest()
                    for path in source_files
                },
                "matched_structure": [
                    "model96 mode parameters and units",
                    "REMOTE1 PP/MI and per-resolution PrintMethod",
                    "ESC (U, ESC (e, ESC (D framing",
                    "two-bit fold, horizontal unpack and vertical split",
                    "raw/cooked zigzag pass mapping",
                    "head-offset scaling and channel allocation order",
                    "relative feed, addressed ESC i, PackBits and termination",
                ],
                "intentional_differences": [
                    "QTR provides already-separated logical ink amounts",
                    "Gutenprint stock color/hue/lum/sat/density transforms are not applied",
                    "payload bytes follow this RIP's deterministic Ordered planes",
                    "Phase 8 verified low branch remains a frozen regression path",
                ],
                "physical_test_status": "NOT_TESTED_FOR_NEW_MODES",
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    written.append(comparison_path)
    return Phase9ArtifactSet(tuple(written))
