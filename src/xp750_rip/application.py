# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Reusable QTR monochrome RIP application service used by CLI and future GUI."""

from __future__ import annotations

import json
import math
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from hashlib import sha256
from pathlib import Path
from time import perf_counter

from xp750_rip.analysis.native_validation import (
    NativeStreamValidation,
    validate_escp2_native,
    validate_xp750_native,
)
from xp750_rip.calibration import decode_per_channel, decode_rgb_selectors
from xp750_rip.contone import LogicalChannelId
from xp750_rip.direction import PrintDirection
from xp750_rip.dither.blue_noise import BlueNoiseFMDither, load_blue_noise_fm_matrix
from xp750_rip.dither.coupled import CoupledErrorDiffusion, coupled_channel_groups
from xp750_rip.dither.error_diffusion import VariableCoefficientErrorDiffusion
from xp750_rip.dither.fast import GutenprintVeryFastDither
from xp750_rip.dither.ordered import (
    GutenprintDropModel,
    GutenprintOrderedDither,
    load_gutenprint_fast_matrix,
    load_gutenprint_matrix,
)
from xp750_rip.dither.strategy import HalftoneAlgorithm, require_implemented_algorithm
from xp750_rip.escp2.backend import (
    BackendRenderResult,
    BackendStreamResult,
    EpsonEscp2Backend,
    GeneratedModeRasterJob,
    resolve_profile_print_method,
)
from xp750_rip.input.tiff import decode_grayscale_tiff
from xp750_rip.layout import ImageSizeRequest, PageLayout, calculate_page_layout
from xp750_rip.performance import PerformanceTrace
from xp750_rip.printers.capabilities import PrinterInstance, PrinterMode
from xp750_rip.qtr.bundle import QtrProfileBundle
from xp750_rip.qtr.raster_executor import (
    black_is_zero_raster_to_qtr_codes,
)
from xp750_rip.qtr.runtime_controls import (
    QtrAdvancedAdjustments,
    QtrRuntimeProfileSet,
    compile_raw_calibration_lut,
    compile_runtime_luts,
)
from xp750_rip.routing import ChannelRouter
from xp750_rip.streaming_raster import streaming_qtr_rasters
from xp750_rip.transport import TransmissionReceipt, Transport


def _resolution_density_factor(mode: PrinterMode) -> float:
    """Apply the printer mode's coverage without an extra paper multiplier."""

    if not math.isfinite(mode.density) or mode.density <= 0:
        raise ValueError(f"Invalid ESC/P2 density for mode {mode.identity}: {mode.density}")
    return min(1.0, mode.density)


def _scale_qtr_luts_for_mode(
    luts: dict[str, tuple[int, ...]], factor: float
) -> dict[str, tuple[int, ...]]:
    if factor == 1.0:
        return luts
    return {
        ink: tuple(min(65535, int(value * factor + 0.5)) for value in samples)
        for ink, samples in luts.items()
    }


@dataclass(frozen=True, slots=True)
class HalftoneConfiguration:
    algorithm: str = "Ordered"
    matrix_id: str = "auto"
    x_offset: int = 0
    y_offset: int = 0


@dataclass(frozen=True, slots=True)
class OutputConfiguration:
    prn_path: Path | None = None
    manifest_path: Path | None = None
    diagnostics_dir: Path | None = None


@dataclass(frozen=True, slots=True)
class CalibrationConfiguration:
    """Explicit raw-target mode; never silently inferred from a TIFF."""

    mode: str  # rgb_selectors | per_channel
    ink_limit_percent: int
    adjustments: QtrAdvancedAdjustments = field(default_factory=QtrAdvancedAdjustments)

    def __post_init__(self) -> None:
        if self.mode not in {"rgb_selectors", "per_channel"}:
            raise ValueError("unknown calibration mode")
        if not 0 <= self.ink_limit_percent <= 100:
            raise ValueError("calibration ink limit must be in range 0..100")
        if not isinstance(self.adjustments, QtrAdvancedAdjustments):
            raise TypeError("calibration adjustments must be QtrAdvancedAdjustments")


@dataclass(frozen=True, slots=True)
class PrintJobConfiguration:
    source_image: Path
    profile_bundle: QtrProfileBundle | None
    target: PrinterInstance
    mode: PrinterMode
    media_id: str
    page_id: str
    input_source_id: str
    image_size: ImageSizeRequest
    active_logical_inks: tuple[LogicalChannelId, ...]
    router: ChannelRouter
    inkset: dict[str, str]
    direction: PrintDirection = PrintDirection.BIDIRECTIONAL
    allow_unsupported_ppd_combinations: bool = False
    runtime_profile_bundles: tuple[QtrProfileBundle, ...] = ()
    qtr_runtime: QtrRuntimeProfileSet | None = None
    halftone: HalftoneConfiguration = HalftoneConfiguration()
    output: OutputConfiguration = OutputConfiguration()
    performance_trace: PerformanceTrace | None = None
    calibration: CalibrationConfiguration | None = None
    device_settings: tuple[tuple[str, str], ...] = ()
    ink_configuration: str | None = None


@dataclass(frozen=True, slots=True)
class RenderedJob:
    data: bytes
    stream_sha256: str | None
    layout: PageLayout
    backend: BackendRenderResult
    validation: NativeStreamValidation
    manifest: dict[str, object]
    diagnostics: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class LiveRenderSummary:
    backend: BackendStreamResult
    stream_size: int
    stream_sha256: str
    manifest: dict[str, object]
    diagnostics: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class LiveNativeValidation:
    passed: bool = True

    def as_dict(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "pass_encoding_completed": True,
            "full_pretransmission_validation": False,
        }


@dataclass(frozen=True, slots=True)
class JobExecutionResult:
    rendered: RenderedJob
    transmission: TransmissionReceipt | None
    prn_path: Path | None
    manifest_path: Path | None
    diagnostics_path: Path | None = None


def _relative_or_absolute(path: Path) -> str:
    return str(path.resolve())


class MonoRipService:
    """Complete file-to-native-stream pipeline with an explicit transport gate."""

    def __init__(
        self,
        backend: EpsonEscp2Backend | None = None,
        *,
        streaming_raster_enabled: bool = True,
    ) -> None:
        self.backend = backend or EpsonEscp2Backend()
        # Internal compatibility/benchmark seam. The product always uses the
        # streaming default; False reproduces the former retained full-page
        # raster path without changing any print parameters.
        self.streaming_raster_enabled = streaming_raster_enabled

    def render(
        self,
        configuration: PrintJobConfiguration,
        *,
        write_chunk: Callable[[bytes], None] | None = None,
        cancel_check: Callable[[], None] | None = None,
    ) -> RenderedJob | LiveRenderSummary:
        trace = configuration.performance_trace
        model = configuration.target.model
        mode = configuration.mode
        medium = model.medium(configuration.media_id)
        page = model.page(configuration.page_id)
        input_source = model.input_source(configuration.input_source_id)
        page = model.native_page(page, mode, input_source)
        halftone_algorithm = require_implemented_algorithm(
            configuration.halftone.algorithm
        )
        calibration = configuration.calibration
        if calibration is None:
            if configuration.profile_bundle is None:
                raise ValueError("A QTR profile is required outside calibration mode")
            runtime = configuration.qtr_runtime or QtrRuntimeProfileSet.single(
                configuration.profile_bundle.quad
            )
            runtime_bundles = configuration.runtime_profile_bundles or (
                configuration.profile_bundle,
            )
            if len(runtime_bundles) != len(runtime.profiles):
                raise ValueError("runtime_profile_bundles не соответствует числу QTR profiles")
        else:
            runtime = None
            runtime_bundles = ()
        if mode not in model.modes:
            raise ValueError(f"PrinterMode {mode.identity} не принадлежит {model.identity}")
        if not mode.implemented:
            raise ValueError(f"Mode {mode.identity} заблокирован: {mode.blocked_reason}")
        if not medium.implemented:
            raise ValueError(f"Media {medium.identity} заблокирован: {medium.blocked_reason}")
        if medium.supported_modes and mode.identity not in medium.supported_modes:
            raise ValueError(
                f"Media {medium.identity} не поддерживает mode {mode.identity}"
            )
        if not page.implemented:
            raise ValueError(f"Page {page.ppd.name} заблокирована: {page.blocked_reason}")
        if not input_source.implemented:
            raise ValueError(
                f"Input source {input_source.identity} заблокирован: "
                f"{input_source.blocked_reason}"
            )
        try:
            model.ink_configuration(configuration.ink_configuration)
        except KeyError as error:
            raise ValueError(
                f"Unknown ink configuration for {model.identity}: "
                f"{configuration.ink_configuration}"
            ) from error
        selected = configuration.active_logical_inks
        if not selected:
            raise ValueError("Не выбран ни один QTR logical ink")
        if calibration is None:
            assert runtime is not None
            profile_inks = set(runtime.logical_inks)
            unknown_inks = tuple(item for item in selected if item.value not in profile_inks)
            if unknown_inks:
                raise ValueError("QUAD не содержит logical inks: " + ", ".join(item.value for item in unknown_inks))
            decoded = decode_grayscale_tiff(configuration.source_image, trace=trace)
            layout = calculate_page_layout(decoded.raster, source_dpi=decoded.dpi, page=page.ppd, mode=mode, request=configuration.image_size, trace=trace)
            calibration_codes = None
        else:
            decoder = decode_rgb_selectors if calibration.mode == "rgb_selectors" else decode_per_channel
            target = decoder(configuration.source_image, tuple(item.value for item in selected), calibration.ink_limit_percent)
            # Reuse the ordinary page mapper for every plane.  This preserves
            # Windows page cropping/resampling while keeping raw code values.
            from xp750_rip.input.tiff import Grayscale8Raster
            proxy = Grayscale8Raster(
                target.width,
                tuple(
                    bytes(255 - max(rows[y][x] for rows in target.rows_by_ink.values()) for x in range(target.width))
                    for y in range(target.height)
                ),
            )
            layout = calculate_page_layout(proxy, source_dpi=target.dpi, page=page.ppd, mode=mode, request=configuration.image_size, trace=trace)
            calibration_codes = {}
            for ink, rows in target.rows_by_ink.items():
                raw = Grayscale8Raster(target.width, tuple(bytes(255 - value for value in row) for row in rows))
                mapped = calculate_page_layout(raw, source_dpi=target.dpi, page=page.ppd, mode=mode, request=configuration.image_size, trace=None)
                calibration_codes[ink] = black_is_zero_raster_to_qtr_codes(mapped.rendered)
            decoded = None

        legacy_canvas = (
            mode.identity == "360x180sw"
            and medium.identity == "PLAIN_NORMAL"
            and input_source.identity == "LowerFront"
        )
        streaming_pipeline = (
            self.streaming_raster_enabled
            and calibration is None
            and not legacy_canvas
        )
        if trace is None:
            code_rows = (
                black_is_zero_raster_to_qtr_codes(layout.rendered)
                if calibration is None and not streaming_pipeline
                else None
            )
        else:
            if configuration.image_size.fit:
                size_request = "fit"
            elif configuration.image_size.native_size:
                size_request = "native-size"
            elif configuration.image_size.scale_percent is not None:
                size_request = "scale-percent"
            elif (
                configuration.image_size.width_mm is not None
                or configuration.image_size.height_mm is not None
            ):
                size_request = "explicit-mm"
            else:
                size_request = "default-fit"
            trace.event(
                "job DATA",
                source_width=layout.source_width_pixels,
                source_height=layout.source_height_pixels,
                source_pixels=layout.source_width_pixels * layout.source_height_pixels,
                requested_width_mm=(
                    configuration.image_size.width_mm
                    if configuration.image_size.width_mm is not None
                    else "auto"
                ),
                requested_height_mm=(
                    configuration.image_size.height_mm
                    if configuration.image_size.height_mm is not None
                    else "auto"
                ),
                size_request=size_request,
                rendered_width_mm=layout.rendered_width_mm,
                rendered_height_mm=layout.rendered_height_mm,
                selected_mode=mode.identity,
                # Historical field names stay available for diagnostic parsers.
                # Explicit aliases distinguish the raster from the printer's
                # coordinate grid in oversampled modes. / Явные имена не дают
                # спутать разрешение растра и координатную сетку принтера.
                printed_dpi=f"{mode.horizontal_dpi}x{mode.vertical_dpi}",
                physical_dpi=(
                    f"{mode.physical_horizontal_dpi}x{mode.physical_vertical_dpi}"
                ),
                raster_resolution_dpi=(
                    f"{mode.horizontal_dpi}x{mode.vertical_dpi}"
                ),
                printer_coordinate_resolution_dpi=(
                    f"{mode.physical_horizontal_dpi}x{mode.physical_vertical_dpi}"
                ),
                resized_width=layout.rendered.width,
                resized_height=layout.rendered.height,
                resized_pixels=layout.rendered.width * layout.rendered.height,
                logical_ink_count=len(runtime.logical_inks) if runtime else len(selected),
                active_logical_ink_count=len(selected),
                bit_depth=mode.bit_depth,
                physical_path_count=len(model.physical_paths),
                expected_passes=mode.horizontal_passes * mode.vertical_passes,
                hpasses=mode.horizontal_passes,
                vpasses=mode.vertical_passes,
            )
            if streaming_pipeline:
                code_rows = None
            else:
                with trace.stage("TIFF -> QTR code conversion"):
                    code_rows = (
                        black_is_zero_raster_to_qtr_codes(layout.rendered)
                        if calibration is None
                        else None
                    )
        # Gutenprint's Fast and VeryFast modes replace the high-quality
        # aspect-aware screen with a small runtime-generated Bayer screen.
        # Adaptive intentionally falls back to Ordered whenever more than one
        # drop size is configured (dither-ed.c); all current Epson modes are
        # variable-drop, so that source-exact behavior is retained here.
        error_diffusion_algorithms = (
            HalftoneAlgorithm.OSTROMOUKHOV,
            HalftoneAlgorithm.ZHOU_FANG,
            HalftoneAlgorithm.ENTROPY_CONSTRAINED_ZHOU_FANG,
            HalftoneAlgorithm.X2Y1,
            HalftoneAlgorithm.COUPLED_ED,
            HalftoneAlgorithm.COUPLED_ED_CM,
        )
        if halftone_algorithm in error_diffusion_algorithms:
            matrix_id = None
            matrix = None
        elif halftone_algorithm in (
            HalftoneAlgorithm.FAST,
            HalftoneAlgorithm.VERY_FAST,
        ):
            matrix_id = "gutenprint-fast-64"
            matrix = load_gutenprint_fast_matrix()
        elif halftone_algorithm is HalftoneAlgorithm.BLUE_NOISE_FM:
            # Printer modes already carry the physical X:Y DPI family used by
            # Gutenprint's ordered screens.  Reuse that family identifier so
            # FM dots stay isotropic on 1x1, 2x1 and 4x1 printer grids.
            matrix_aspect = mode.ordered_matrix_id.removeprefix("matrix-")
            matrix_id = f"pytone-vac-blue-noise-{matrix_aspect}"
            matrix = load_blue_noise_fm_matrix(mode.ordered_matrix_id)
        else:
            matrix_id = mode.ordered_matrix_id
            matrix = load_gutenprint_matrix(matrix_id)
        dither_width = (
            layout.canvas_width_pixels
            if legacy_canvas
            else (layout.rendered.width + 7) // 8 * 8
        )
        dither_height = layout.canvas_height_rows if legacy_canvas else layout.rendered.height
        drops = GutenprintDropModel.from_drop_sizes(
            mode.drop_sizes, raster_bit_depth=mode.bit_depth
        )
        x_offset = configuration.halftone.x_offset + (
            0 if legacy_canvas else layout.placement_x_pixels
        )
        y_offset = configuration.halftone.y_offset + (
            0 if legacy_canvas else layout.placement_y_rows
        )
        if halftone_algorithm.is_coupled:
            dither = CoupledErrorDiffusion(
                drops=drops, x_offset=x_offset, y_offset=y_offset,
                couple_light_inks=halftone_algorithm is HalftoneAlgorithm.COUPLED_ED,
            )
        elif halftone_algorithm in error_diffusion_algorithms:
            dither = VariableCoefficientErrorDiffusion(
                drops=drops,
                algorithm=halftone_algorithm,
                x_offset=x_offset,
                y_offset=y_offset,
            )
        else:
            if halftone_algorithm is HalftoneAlgorithm.VERY_FAST:
                dither_class = GutenprintVeryFastDither
            elif halftone_algorithm is HalftoneAlgorithm.BLUE_NOISE_FM:
                dither_class = BlueNoiseFMDither
            else:
                dither_class = GutenprintOrderedDither
            dither = dither_class(
                matrix=matrix,
                drops=drops,
                x_offset=x_offset,
                y_offset=y_offset,
            )
        effective_dither = (
            HalftoneAlgorithm.ORDERED.value
            if halftone_algorithm is HalftoneAlgorithm.ADAPTIVE_HYBRID
            else halftone_algorithm.value
        )
        dither_stage_name = f"{halftone_algorithm.value} dither"
        if effective_dither != halftone_algorithm.value:
            dither_stage_name += f" ({effective_dither} fallback)"
        if calibration is not None:
            raw_lut = compile_raw_calibration_lut(calibration.adjustments)
            runtime_luts = {
                logical.value: raw_lut
                for logical in selected
            }
        elif runtime.is_neutral_single_profile:
            runtime_luts = {
                logical.value: tuple(runtime.profiles[0].curve(logical.value).samples)
                for logical in selected
            }
        else:
            runtime_luts = compile_runtime_luts(runtime)
        # Normal and calibration printing share the mode's coverage factor.
        # Paper selection must not add another reduction to the curve values.
        resolution_density_factor = _resolution_density_factor(mode)
        runtime_luts = _scale_qtr_luts_for_mode(
            runtime_luts, resolution_density_factor
        )
        if streaming_pipeline:
            def report_dither_start() -> None:
                assert trace is not None
                trace.event(
                    f"Streaming Cython QTR + {dither_stage_name} START"
                )

            physical = streaming_qtr_rasters(
                source_rows=layout.rendered.rows,
                logical_inks=selected,
                routing=configuration.router.mapping,
                qtr_luts=runtime_luts,
                dither=dither,
                canvas_width=dither_width,
                available_paths=tuple(
                    definition.identity for definition in model.physical_paths
                ),
                allow_multiple_logical_to_one_path=(
                    configuration.router.allow_multiple_logical_to_one_path
                ),
                cancel_check=cancel_check if write_chunk is not None else None,
                on_dither_start=report_dither_start if trace is not None else None,
            )
            if trace is not None:
                trace.event(
                    "Streaming QTR raster DATA",
                    algorithm=halftone_algorithm.value,
                    effective_algorithm=effective_dither,
                    band_rows=256,
                    logical_ink_count=len(selected),
                    full_code_raster_materialized=False,
                    full_dot_planes_materialized=False,
                )
        else:
            def dither_logical_ink(logical_id: LogicalChannelId):
                source = (
                    calibration_codes[logical_id.value]
                    if calibration_codes is not None
                    else code_rows
                )
                assert source is not None
                return dither.dither_qtr_codes_placed(
                    source,
                    runtime_luts[logical_id.value],
                    identity=logical_id,
                    canvas_width=dither_width,
                    canvas_height=dither_height,
                    x=layout.placement_x_pixels if legacy_canvas else 0,
                    y=layout.placement_y_rows if legacy_canvas else 0,
                )

            parallel_error_diffusion = (
                halftone_algorithm in error_diffusion_algorithms and len(selected) > 1
            )
            if isinstance(dither, CoupledErrorDiffusion):
                dots = dither.dither_channels_placed(
                    {ink: calibration_codes[ink.value] if calibration_codes is not None
                     else code_rows for ink in selected},
                    runtime_luts, selected, canvas_width=dither_width, canvas_height=dither_height,
                    x=layout.placement_x_pixels if legacy_canvas else 0,
                    y=layout.placement_y_rows if legacy_canvas else 0,
                )
            elif parallel_error_diffusion:
                worker_count = min(len(selected), max(1, (os.cpu_count() or 2) // 2))
                if trace is not None:
                    trace.event(
                        f"Parallel Cython QTR + {dither_stage_name} START",
                        logical_ink_count=len(selected),
                        workers=worker_count,
                    )
                parallel_started = perf_counter()
                with ThreadPoolExecutor(
                    max_workers=worker_count,
                    thread_name_prefix="pytone-ed",
                ) as executor:
                    dots = list(executor.map(dither_logical_ink, selected))
                if trace is not None:
                    trace.record_duration(
                        f"Parallel Cython QTR + {dither_stage_name}",
                        perf_counter() - parallel_started,
                        logical_ink_count=len(selected),
                        workers=worker_count,
                    )
            else:
                dots = []
                for logical_id in selected:
                    if trace is None:
                        dot_plane = dither_logical_ink(logical_id)
                    else:
                        with trace.stage(
                            f"Cython fused QTR + {dither_stage_name} "
                            f"{logical_id.value}"
                        ):
                            dot_plane = dither_logical_ink(logical_id)
                    dots.append(dot_plane)
            if trace is None:
                physical = configuration.router.route(
                    tuple(dots),
                    available_paths=tuple(item.identity for item in model.physical_paths),
                )
            else:
                with trace.stage("ChannelRouter"):
                    physical = configuration.router.route(
                        tuple(dots),
                        available_paths=tuple(item.identity for item in model.physical_paths),
                        trace=trace,
                    )
        live_backend: BackendStreamResult | None = None
        live_digest = sha256()
        live_size = 0
        if write_chunk is not None:
            if cancel_check is not None:
                cancel_check()
            def write_and_hash(chunk: bytes) -> None:
                nonlocal live_size
                write_chunk(chunk)
                live_digest.update(chunk)
                live_size += len(chunk)

            live_backend = self.backend.render_stream(
                model=model,
                mode=mode,
                medium=medium,
                page=page,
                input_source=input_source,
                rasters=physical,
                write_chunk=write_and_hash,
                cancel_check=cancel_check,
                direction=configuration.direction,
                allow_unsupported_ppd_combinations=(
                    configuration.allow_unsupported_ppd_combinations
                ),
                horizontal_offset_pixels=0 if legacy_canvas else layout.placement_x_pixels,
                vertical_offset_rows=0 if legacy_canvas else layout.placement_y_rows,
                device_settings=dict(configuration.device_settings),
                ink_configuration=configuration.ink_configuration,
            )
            backend_result = BackendRenderResult(
                job=GeneratedModeRasterJob(data=b"", rasters=physical, passes=()),
                model=model,
                mode=mode,
                medium=medium,
                page=page,
                input_source=input_source,
                ppd_constraints_checked=model.ppd.constraints,
                page_formula_classification=live_backend.page_formula_classification,
            )
        else:
            backend_result = self.backend.render(
            model=model,
            mode=mode,
            medium=medium,
            page=page,
            input_source=input_source,
            rasters=physical,
            direction=configuration.direction,
            allow_unsupported_ppd_combinations=(
                configuration.allow_unsupported_ppd_combinations
            ),
            horizontal_offset_pixels=0 if legacy_canvas else layout.placement_x_pixels,
            vertical_offset_rows=0 if legacy_canvas else layout.placement_y_rows,
            device_settings=dict(configuration.device_settings),
            ink_configuration=configuration.ink_configuration,
            trace=trace,
            )
        validator = (
            validate_escp2_native
            if model.escp2_profile is not None
            and model.backend_family != self.backend.family
            else validate_xp750_native
        )
        if live_backend is not None:
            validation = LiveNativeValidation()
        elif trace is None:
            validation = validator(backend_result)
        else:
            with trace.stage("native validation"):
                validation = validator(backend_result)
        hash_required = (
            configuration.output.manifest_path is not None
            or configuration.output.diagnostics_dir is not None
        )
        if not hash_required:
            digest = None
            image_digest = None
        elif live_backend is not None:
            digest = live_digest.hexdigest()
            image_hash = sha256()
            with configuration.source_image.open("rb") as image_file:
                for image_chunk in iter(lambda: image_file.read(1024 * 1024), b""):
                    image_hash.update(image_chunk)
            image_digest = image_hash.hexdigest()
        elif trace is None:
            digest = sha256(backend_result.data).hexdigest()
            image_digest = sha256(configuration.source_image.read_bytes()).hexdigest()
        else:
            with trace.stage("SHA-256 computation"):
                digest = sha256(backend_result.data).hexdigest()
                image_digest = sha256(configuration.source_image.read_bytes()).hexdigest()
        mapping = {
            logical.value: configuration.router.mapping[logical].value for logical in selected
        }
        manifest_started = perf_counter()
        manifest_print_method = (
            resolve_profile_print_method(
                model, mode, medium, dict(configuration.device_settings)
            )
            if model.escp2_profile is not None
            and model.backend_family != self.backend.family
            else medium.print_method(mode.identity)
            if medium.implemented
            else None
        )
        if trace is not None:
            trace.event("manifest construction START")
        calibration_runtime_manifest = (
            {
                "model": "raw calibration; QUAD curves and blending bypassed",
                "profile_count": 0,
                "calibration_mode": calibration.mode,
                "ink_limit_percent": calibration.ink_limit_percent,
                "gamma": calibration.adjustments.gamma,
                "normal_ink_limit": calibration.adjustments.ink_limit,
            }
            if calibration is not None
            else None
        )
        manifest: dict[str, object] = {
            "schema": "epson-rip-job-manifest-v2-lightweight",
            "source_image": _relative_or_absolute(configuration.source_image),
            "source_image_sha256": image_digest,
            "source_dimensions_pixels": [layout.source_width_pixels, layout.source_height_pixels],
            "rendered_dimensions_pixels": [
                layout.rendered.width,
                layout.rendered.height,
            ],
            "rendered_dimensions_mm": [
                layout.rendered_width_mm,
                layout.rendered_height_mm,
            ],
            "tiff": {
                "bits_per_sample": decoded.bits_per_sample if decoded else 8,
                "photometric_interpretation": decoded.photometric_interpretation if decoded else None,
                "orientation": decoded.orientation if decoded else 1,
                "compression": decoded.compression if decoded else None,
                "icc_present": decoded.has_icc_profile if decoded else False,
                "dpi": list(decoded.dpi) if decoded and decoded.dpi else None,
            },
            "qtr_polarity_rule": "qtr_code = 255 - TIFF_sample",
            "print_direction": configuration.direction.value,
            "quad_path": str(configuration.profile_bundle.quad_path) if configuration.profile_bundle else None,
            "quad_sha256": configuration.profile_bundle.quad_sha256 if configuration.profile_bundle else None,
            "profile_warnings": list(configuration.profile_bundle.warnings) if configuration.profile_bundle else [],
            "qtr_runtime": calibration_runtime_manifest or {
                "model": "EpsonRIP-defined transparent runtime v1",
                "profile_count": len(runtime.profiles),
                "profiles": [
                    {
                        "path": str(bundle.quad_path),
                        "sha256": bundle.quad_sha256,
                        "size_bytes": bundle.source_size_bytes,
                        "modified_ns": bundle.source_modified_ns,
                        "cache": "none; fresh file snapshot per job",
                        "logical_inks": list(bundle.logical_inks),
                    }
                    for bundle in runtime_bundles
                ],
                "split_tone": runtime.blend.split_tone,
                "anchors": {"highlights": 0, "midtones": 128, "shadows": 255},
                "weights": {
                    "highlights": list(runtime.blend.weights.highlights),
                    "midtones": list(runtime.blend.weights.midtones),
                    "shadows": list(runtime.blend.weights.shadows),
                },
                "gamma": runtime.adjustments.gamma,
                "ink_limit": runtime.adjustments.ink_limit,
                "compatibility_claim": "EPSONRIP_DEFINED_NOT_CLOSED_QTR_BYTE_EXACT",
            },
            "evidence_classification": {
                "quad_format_and_runtime_semantics": "FORMAT/SEMANTICS-GENERIC",
                "profile_curve_values": "compiled .quad runtime data",
                "logical_to_physical_routing": "explicit job configuration",
                "native_backend": model.backend_family,
                "non_letter_page_formula": backend_result.page_formula_classification,
            },
            "logical_channel_count": (
                len(runtime.logical_inks) if runtime is not None else len(selected)
            ),
            "profile_logical_channels": list(runtime.logical_inks) if runtime else [item.value for item in selected],
            "active_logical_channel_count": len(selected),
            "active_logical_channels": [item.value for item in selected],
            "routing": mapping,
            "inkset": dict(configuration.inkset),
            "ink_configuration": configuration.ink_configuration,
            "printer": {
                "model": model.identity,
                "display_name": model.display_name,
                "path_count": len(model.physical_paths),
                "active_physical_paths": list(mapping.values()),
                "backend": model.backend_family,
            },
            "mode": {
                "id": mode.identity,
                "resolution_dpi": [mode.horizontal_dpi, mode.vertical_dpi],
                "raster_resolution_dpi": [mode.horizontal_dpi, mode.vertical_dpi],
                "implemented": mode.implemented,
                "offline_validated": mode.offline_validated,
                "drop_sizes": list(mode.drop_sizes),
                "bit_depth": mode.bit_depth,
                "physical_resolution_dpi": [
                    mode.physical_horizontal_dpi,
                    mode.physical_vertical_dpi,
                ],
                "printer_coordinate_resolution_dpi": [
                    mode.physical_horizontal_dpi,
                    mode.physical_vertical_dpi,
                ],
                "base_resolution_dpi": mode.base_resolution_dpi,
                "horizontal_oversampling": mode.horizontal_passes,
                "vertical_passes": mode.vertical_passes,
                "nozzles": mode.nozzle_count,
                "nozzle_separation": mode.nozzle_separation,
                "escp2_ink_type": mode.escp2_ink_type,
                "density": mode.density,
                "applied_qtr_density_factor": resolution_density_factor,
                "source": mode.source,
                "blocked_reason": mode.blocked_reason,
                "ordered_matrix_id": mode.ordered_matrix_id,
            },
            "media": medium.identity,
            "media_backend": {
                "id": medium.identity,
                "gutenprint_name": medium.gutenprint_name,
                "paper_media_code": medium.media_code,
                "print_method": (
                    f"0x{manifest_print_method:02X}"
                    if manifest_print_method is not None
                    else None
                ),
                "print_method_payload_length": (
                    mode.escp2_print_method_payload_length
                    if manifest_print_method is not None else None
                ),
            },
            "page": {
                "id": page.ppd.name,
                "physical_points": [page.ppd.width_points, page.ppd.height_points],
                "physical_mm": [page.ppd.width_mm, page.ppd.height_mm],
                "imageable_area_points": [
                    page.ppd.imageable_left_points,
                    page.ppd.imageable_bottom_points,
                    page.ppd.imageable_right_points,
                    page.ppd.imageable_top_points,
                ],
                "borderless": page.ppd.borderless,
                "implemented": page.implemented,
                "offline_validated": page.offline_validated,
                "backend_page_formula_classification": backend_result.page_formula_classification,
            },
            "placement_pixels": [
                layout.placement_x_pixels,
                layout.placement_y_rows,
            ],
            "printable_canvas_pixels": [
                layout.canvas_width_pixels,
                layout.canvas_height_rows,
            ],
            "input_source": input_source.identity,
            "halftone": {
                "algorithm": configuration.halftone.algorithm,
                "gutenprint_identifier": halftone_algorithm.gutenprint_identifier,
                "matrix": matrix_id,
                "requested_matrix": configuration.halftone.matrix_id,
                "matrix_source": (
                    matrix.source
                    if matrix is not None
                    else "coupled adaptive error diffusion; no threshold matrix"
                    if halftone_algorithm.is_coupled
                    else "constant-weight X2Y1 error diffusion; no threshold matrix"
                    if halftone_algorithm is HalftoneAlgorithm.X2Y1
                    else "variable-coefficient error diffusion; no threshold matrix"
                ),
                "matrix_dimensions": (
                    [matrix.width, matrix.height] if matrix is not None else None
                ),
                "matrix_offset": [
                    configuration.halftone.x_offset,
                    configuration.halftone.y_offset,
                ],
                "serpentine": (
                    True if halftone_algorithm in error_diffusion_algorithms else None
                ),
                "blank_region_policy": (
                    "per-group zero demand; settle 3 rows, clear on fourth; keep LCG state"
                    if halftone_algorithm.is_coupled else
                    "per-ink zero demand; settle 3 rows, clear on fourth"
                    if halftone_algorithm in error_diffusion_algorithms
                    else "skip zero ink demand; preserve absolute screen coordinates"
                ),
                "coefficient_source": (
                    "ReferenceDriver adaptive signed four-neighbour recurrence"
                    if halftone_algorithm.is_coupled else
                    "X2Y1: ahead 11/19, next-row back 3/19, centre 4/19, ahead 1/19"
                    if halftone_algorithm is HalftoneAlgorithm.X2Y1 else
                    "Ostromoukhov SIGGRAPH 2001 Appendix I"
                    if halftone_algorithm is HalftoneAlgorithm.OSTROMOUKHOV
                    else (
                        "Zhou-Fang SIGGRAPH 2003 Table 1"
                        if halftone_algorithm in (
                            HalftoneAlgorithm.ZHOU_FANG,
                            HalftoneAlgorithm.ENTROPY_CONSTRAINED_ZHOU_FANG,
                        )
                        else None
                    )
                ),
                "threshold_modulation": (
                    "Noise_NatInv: 65 signed int16 amplitude knots; natural-inverse interpolation; "
                    "MT19937 shuffled signed 15-bit samples; continuous per-ink cursor"
                    if halftone_algorithm is HalftoneAlgorithm.X2Y1 else
                    "conditional LCG 0x343FD/0x269EC3; level-dependent amplitude"
                    if halftone_algorithm.is_coupled else
                    "Liu et al. 2014 entropy-constrained Zhou-Fang"
                    if halftone_algorithm
                    is HalftoneAlgorithm.ENTROPY_CONSTRAINED_ZHOU_FANG
                    else (
                        "Zhou-Fang 2003 Equation 1 and Table 2"
                        if halftone_algorithm is HalftoneAlgorithm.ZHOU_FANG
                        else None
                    )
                ),
                "execution": (
                    "AOT Cython specialized single/pair adaptive ED"
                    if halftone_algorithm.is_coupled else
                    "Gutenprint variable-drop Adaptive fallback to Ordered"
                    if halftone_algorithm is HalftoneAlgorithm.ADAPTIVE_HYBRID
                    else (
                        "AOT Cython serpentine X2Y1 error diffusion"
                        if halftone_algorithm is HalftoneAlgorithm.X2Y1 else
                        "AOT Cython serpentine variable-coefficient error diffusion"
                        if halftone_algorithm in error_diffusion_algorithms
                        else "native selected algorithm"
                    )
                ),
                "screen_tiling": (
                    "deterministic aperiodic VAC phase/reflection per tile and ink"
                    if halftone_algorithm is HalftoneAlgorithm.BLUE_NOISE_FM
                    else None
                ),
                "coupled_channel_groups": (
                    [[ink.value for ink in group] for group in coupled_channel_groups(
                         selected, couple_light_inks=dither.couple_light_inks)
                     if len(group) == 2]
                    if halftone_algorithm.is_coupled else []
                ),
            },
            "stream_size": live_size if live_backend is not None else len(backend_result.data),
            "stream_sha256": digest,
            "validation": validation.as_dict(),
            "transmission": {"status": "NOT_SENT", "bytes_sent": 0, "destination": None},
            "printer_access_performed_during_render": False,
            "network_access_performed_during_render": False,
            "processing_raster_pixels": [physical.width_pixels, physical.height_rows],
            "memory_strategy": (
                "bounded_qtr_dither_weave_and_native_output"
                if live_backend is not None and streaming_pipeline
                else "bounded_qtr_dither_bands_before_softweave"
                if streaming_pipeline
                else (
                    "phase8_full_printable_canvas_regression"
                    if legacy_canvas
                    else (
                        "retained_per_channel_calibration_rasters"
                        if calibration is not None
                        else "retained_full_page_compatibility"
                    )
                )
            ),
        }
        diagnostics: dict[str, object] | None = None
        if (
            configuration.output.diagnostics_dir is not None
            and backend_result.weave_schedule is not None
        ):
            weave_details: dict[str, object] = {
                "source": "Gutenprint 5.3.5 print-weave.c/stpi_escp2_flush_pass",
                "first_line": backend_result.weave_schedule.first_line,
                "page_height_rows": backend_result.weave_schedule.page_height_rows,
                "pass_count": len(backend_result.weave_schedule.passes),
                "passes": [
                    {
                        "pass": item.pass_index,
                        "logical_pass_start": item.logical_pass_start,
                        "physical_pass_start": item.physical_pass_start,
                        "physical_pass_end": item.physical_pass_end,
                        "subpass": item.subpass,
                        "vertical_advance_units": item.vertical_advance,
                        "active_physical_paths": [block.path.value for block in item.blocks],
                        "raster_block_count": len(item.blocks),
                        "blocks": [
                            {
                                "physical_path": block.path.value,
                                "selector": f"0x{block.raster.selector:02X}",
                                "rows": block.raster.row_count,
                                "horizontal_position_units": block.raster.horizontal_position,
                                "nozzle_rows": list(block.raster.nozzle_rows),
                                "source_rows": list(block.raster.source_rows),
                            }
                            for block in item.blocks
                        ],
                    }
                    for item in backend_result.weave_schedule.passes
                ],
            }
            diagnostics = {
                "schema": "epson-rip-job-diagnostics-v1",
                "manifest": manifest,
                "weave": weave_details,
            }
        elif configuration.output.diagnostics_dir is not None and live_backend is not None:
            diagnostics = {
                "schema": "epson-rip-job-diagnostics-v1",
                "manifest": manifest,
                "weave": {
                    "source": "bounded streaming weave",
                    "pass_count": live_backend.pass_count,
                    "passes": "not retained in online mode",
                },
            }
        elif configuration.output.diagnostics_dir is not None:
            weave_details = {
                "source": "XP750-VERIFIED Phase 5 fixed 360x180 band scheduler",
                "pass_count": len(backend_result.job.passes),
                "passes": [
                    {
                        "pass": item.pass_index,
                        "vertical_advance_units": item.vertical_advance,
                        "active_physical_paths": [block.path.value for block in item.blocks],
                        "raster_block_count": len(item.blocks),
                        "blocks": [
                            {
                                "physical_path": block.path.value,
                                "selector": f"0x{block.raster.selector:02X}",
                                "rows": block.raster.row_count,
                            }
                            for block in item.blocks
                        ],
                    }
                    for item in backend_result.job.passes
                ],
            }
            diagnostics = {
                "schema": "epson-rip-job-diagnostics-v1",
                "manifest": manifest,
                "weave": weave_details,
            }
        if trace is not None:
            trace.record_duration("manifest construction", perf_counter() - manifest_started)
        if live_backend is not None:
            manifest["delivery_mode"] = "online"
            manifest["native_stream_emitted_incrementally"] = True
            manifest["transmission"] = {
                "status": "EMITTED_TO_SINK",
                "bytes_sent": live_size,
                "destination": None,
            }
            return LiveRenderSummary(
                backend=live_backend,
                stream_size=live_size,
                stream_sha256=live_digest.hexdigest(),
                manifest=manifest,
                diagnostics=diagnostics,
            )
        return RenderedJob(
            data=bytes(backend_result.data),
            stream_sha256=digest,
            layout=layout,
            backend=backend_result,
            validation=validation,
            manifest=manifest,
            diagnostics=diagnostics,
        )

    def execute(
        self,
        configuration: PrintJobConfiguration,
        *,
        send: bool = False,
        transport: Transport | None = None,
    ) -> JobExecutionResult:
        rendered = self.render(configuration)
        trace = configuration.performance_trace
        if not rendered.validation.passed:
            raise ValueError("Передача запрещена: native stream не прошёл validation")
        if configuration.output.prn_path is not None:
            if trace is None:
                configuration.output.prn_path.parent.mkdir(parents=True, exist_ok=True)
                configuration.output.prn_path.write_bytes(rendered.data)
            else:
                with trace.stage("PRN write", bytes=len(rendered.data)):
                    configuration.output.prn_path.parent.mkdir(parents=True, exist_ok=True)
                    configuration.output.prn_path.write_bytes(rendered.data)

        receipt: TransmissionReceipt | None = None
        if send:
            if transport is None:
                raise ValueError("--send требует настроенный Transport")
            if trace is None:
                receipt = transport.send(rendered.data)
            else:
                with trace.stage("transport", bytes=len(rendered.data)):
                    receipt = transport.send(rendered.data)
            if receipt.bytes_sent != len(rendered.data):
                raise ValueError("Transport сообщил неполную передачу validated bytes")
        transmission = {
            "status": "SENT" if receipt else "NOT_SENT",
            "bytes_sent": receipt.bytes_sent if receipt else 0,
            "destination": receipt.destination if receipt else None,
        }
        final_manifest = dict(rendered.manifest)
        final_manifest["transmission"] = transmission
        rendered = replace(rendered, manifest=final_manifest)
        if configuration.output.manifest_path is not None:
            if trace is None:
                serialized = json.dumps(final_manifest, ensure_ascii=False, indent=2) + "\n"
                configuration.output.manifest_path.parent.mkdir(parents=True, exist_ok=True)
                configuration.output.manifest_path.write_text(serialized, encoding="utf-8")
            else:
                with trace.stage("JSON serialization"):
                    serialized = (
                        json.dumps(final_manifest, ensure_ascii=False, indent=2) + "\n"
                    )
                with trace.stage("manifest write", bytes=len(serialized.encode("utf-8"))):
                    configuration.output.manifest_path.parent.mkdir(
                        parents=True, exist_ok=True
                    )
                    configuration.output.manifest_path.write_text(
                        serialized, encoding="utf-8"
                    )
        diagnostics_path: Path | None = None
        if configuration.output.diagnostics_dir is not None:
            stem = (
                configuration.output.prn_path.stem
                if configuration.output.prn_path is not None
                else "job"
            )
            diagnostics_path = configuration.output.diagnostics_dir / f"{stem}.diagnostics.json"
            final_diagnostics = dict(rendered.diagnostics or {})
            final_diagnostics["manifest"] = final_manifest
            if trace is None:
                diagnostics_serialized = (
                    json.dumps(final_diagnostics, ensure_ascii=False, indent=2) + "\n"
                )
                diagnostics_path.parent.mkdir(parents=True, exist_ok=True)
                diagnostics_path.write_text(diagnostics_serialized, encoding="utf-8")
            else:
                with trace.stage("diagnostics JSON serialization"):
                    diagnostics_serialized = (
                        json.dumps(final_diagnostics, ensure_ascii=False, indent=2) + "\n"
                    )
                with trace.stage(
                    "diagnostics write",
                    bytes=len(diagnostics_serialized.encode("utf-8")),
                ):
                    diagnostics_path.parent.mkdir(parents=True, exist_ok=True)
                    diagnostics_path.write_text(diagnostics_serialized, encoding="utf-8")
        return JobExecutionResult(
            rendered=rendered,
            transmission=receipt,
            prn_path=configuration.output.prn_path,
            manifest_path=configuration.output.manifest_path,
            diagnostics_path=diagnostics_path,
        )
