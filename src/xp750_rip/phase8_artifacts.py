# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Reproducible, file-only Phase 8 XP-750 candidate generation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from xp750_rip.application import MonoRipService, OutputConfiguration, PrintJobConfiguration
from xp750_rip.contone import LogicalChannelId
from xp750_rip.layout import ImageSizeRequest
from xp750_rip.printers.capabilities import PrinterInstance
from xp750_rip.printers.xp750_capabilities import load_xp750_model
from xp750_rip.qtr.bundle import load_profile_bundle
from xp750_rip.routing import ChannelRouter


@dataclass(frozen=True, slots=True)
class Phase8ArtifactSet:
    paths: tuple[Path, ...]


def _prepare_grayscale_photo(source: Path, destination: Path) -> None:
    """Create a declared test fixture; this is not an RIP RGB input feature."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as image:
        image.convert("L").save(
            destination,
            format="TIFF",
            compression="tiff_lzw",
            dpi=(360, 360),
        )


def generate_phase8_artifacts(
    output_root: Path,
    *,
    workspace_root: Path | None = None,
) -> Phase8ArtifactSet:
    """Generate incremental, photographic and per-implemented-mode validated PRNs."""

    root = workspace_root or Path(__file__).resolve().parents[3]
    quad_path = root / "Docs" / "OCBESTpk-PripaGlossy-InkMix-smooth-neutr.quad"
    step_image = root / "Docs" / "QuadToneRIP" / "21step.tif"
    photo_source = root / "gutenprint-5.3.5" / "samples" / "profile.jpg"
    photo_tiff = output_root / "photo_candidates" / "gutenprint_profile_grayscale.tif"
    _prepare_grayscale_photo(photo_source, photo_tiff)

    model = load_xp750_model()
    instance = PrinterInstance(model=model, name="Phase8 XP-750 file-only")
    mode = model.mode("360x180sw")
    bundle = load_profile_bundle(quad_path=quad_path)
    profile_inks = tuple(LogicalChannelId(item) for item in bundle.logical_inks)
    available_paths = tuple(item.identity for item in model.physical_paths)
    if len(profile_inks) < 6 or len(available_paths) < 6:
        raise ValueError("XP-750 Phase 8 incremental fixture требует по меньшей мере 6 inks/paths")
    explicit_routes = {
        logical: path for logical, path in zip(profile_inks[:6], available_paths[:6], strict=True)
    }
    service = MonoRipService()
    written: list[Path] = [photo_tiff]

    def render_candidate(
        directory: Path,
        stem: str,
        source: Path,
        width_mm: float,
        count: int,
    ) -> None:
        prn = directory / f"{stem}.prn"
        manifest = directory / f"{stem}.json"
        validation = directory / f"{stem}_validation.json"
        active = profile_inks[:count]
        result = service.execute(
            PrintJobConfiguration(
                source_image=source,
                profile_bundle=bundle,
                target=instance,
                mode=mode,
                media_id="PLAIN_NORMAL",
                page_id="Letter",
                input_source_id="LowerFront",
                image_size=ImageSizeRequest(width_mm=width_mm, center=True),
                active_logical_inks=active,
                router=ChannelRouter({logical: explicit_routes[logical] for logical in active}),
                inkset={},
                output=OutputConfiguration(prn, manifest),
            ),
            send=False,
        )
        validation.parent.mkdir(parents=True, exist_ok=True)
        validation.write_text(
            json.dumps(
                {
                    "validation": result.rendered.validation.as_dict(),
                    "stream_sha256": result.rendered.stream_sha256,
                    "mode_source": mode.source,
                    "expected_physical_mm": result.rendered.manifest[
                        "rendered_dimensions_mm"
                    ],
                    "transmission": "NOT_SENT",
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        written.extend((prn, manifest, validation))

    incremental = output_root / "phase8_qtr_incremental"
    for count in range(1, 7):
        render_candidate(incremental, f"xp750_qtr_{count}path", step_image, 60.0, count)

    photo_dir = output_root / "photo_candidates"
    for label, width, count in (
        ("small_40mm_1path", 40.0, 1),
        ("medium_100mm_6path", 100.0, 6),
        ("near_width_175mm_1path", 175.0, 1),
    ):
        render_candidate(photo_dir, f"xp750_photo_{label}", photo_tiff, width, count)

    # Every newly implemented XP-750 mode: currently exactly one honest mode.
    mode_dir = output_root / "mode_tests"
    for implemented_mode in (item for item in model.modes if item.implemented):
        if implemented_mode.identity != mode.identity:
            raise ValueError("Artifact generator должен явно поддержать новый implemented mode")
        render_candidate(
            mode_dir,
            f"xp750_{implemented_mode.identity}_diagnostic",
            step_image,
            60.0,
            1,
        )

    provenance = output_root / "photo_candidates" / "SOURCE.json"
    provenance.write_text(
        json.dumps(
            {
                "source": str(photo_source.resolve()),
                "derived_tiff": str(photo_tiff.resolve()),
                "conversion": "Pillow RGB -> L, LZW TIFF, 360 dpi; offline test fixture only",
                "rip_rgb_input_support": False,
                "purpose": "реальное фотографическое содержимое для Phase 8 layout/RIP candidates",
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    written.append(provenance)
    return Phase8ArtifactSet(tuple(written))
