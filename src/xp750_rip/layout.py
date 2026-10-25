# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Printer-neutral grayscale resizing, physical sizing and page placement."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from math import floor
from typing import TYPE_CHECKING

from PIL import Image

from xp750_rip.capabilities.ppd import PpdPage
from xp750_rip.input.tiff import Grayscale8Raster
from xp750_rip.printers.capabilities import PrinterMode
from xp750_rip.resampling import resize_mitchell

if TYPE_CHECKING:
    from xp750_rip.performance import PerformanceTrace


class ResamplingMethod(StrEnum):
    NEAREST = "nearest"
    BILINEAR = "bilinear"
    BICUBIC = "bicubic"
    LANCZOS = "lanczos"
    MITCHELL = "mitchell"


class LayoutOrientation(StrEnum):
    PORTRAIT = "portrait"
    LANDSCAPE = "landscape"
    AUTO = "auto"


_PIL_RESAMPLING = {
    ResamplingMethod.NEAREST: Image.Resampling.NEAREST,
    ResamplingMethod.BILINEAR: Image.Resampling.BILINEAR,
    ResamplingMethod.BICUBIC: Image.Resampling.BICUBIC,
    ResamplingMethod.LANCZOS: Image.Resampling.LANCZOS,
}


def _resize_image(
    image: Image.Image, size: tuple[int, int], method: ResamplingMethod,
) -> Image.Image:
    if isinstance(image, Grayscale8Raster):
        from xp750_rip.precision import resize_grayscale_rows
        return resize_grayscale_rows(image, size[0], size[1], method)
    if image.size == size:
        return image
    if method is ResamplingMethod.MITCHELL:
        return resize_mitchell(image, size)
    return image.resize(size, resample=_PIL_RESAMPLING[method])


@dataclass(frozen=True, slots=True)
class ImageSizeRequest:
    width_mm: float | None = None
    height_mm: float | None = None
    scale_percent: float | None = None
    fit: bool = False
    native_size: bool = False
    resampling: ResamplingMethod = ResamplingMethod.LANCZOS
    orientation: LayoutOrientation = LayoutOrientation.AUTO
    center: bool = True
    x_mm: float | None = None
    y_mm: float | None = None
    # True only for a full physical-page raster produced by Windows XpsToTiff.
    # Such a raster is already composed and must not go through image layout.
    windows_page: bool = False


@dataclass(frozen=True, slots=True)
class PageLayout:
    page_name: str
    source_width_pixels: int
    source_height_pixels: int
    rendered: Grayscale8Raster
    rendered_width_mm: float
    rendered_height_mm: float
    canvas_width_pixels: int
    canvas_height_rows: int
    placement_x_pixels: int
    placement_y_rows: int
    printable_width_mm: float
    printable_height_mm: float
    orientation: LayoutOrientation
    resampling: ResamplingMethod


def _rotate_for_orientation(
    raster: Grayscale8Raster,
    orientation: LayoutOrientation,
    dpi: tuple[float, float] | None,
) -> tuple[Grayscale8Raster, tuple[float, float] | None]:
    rotate = (
        orientation is LayoutOrientation.PORTRAIT
        and raster.width > raster.height
        or orientation is LayoutOrientation.LANDSCAPE
        and raster.height > raster.width
    )
    if not rotate:
        return raster, dpi
    from xp750_rip.input.tiff_rows import RotatedRows
    return type(raster)(raster.height, RotatedRows(raster)), ((dpi[1], dpi[0]) if dpi else None)


def resize_grayscale(
    raster: Grayscale8Raster,
    *,
    width: int,
    height: int,
    method: ResamplingMethod,
) -> Grayscale8Raster:
    if width <= 0 or height <= 0:
        raise ValueError("Размер resampled raster должен быть положительным")
    if (width, height) == (raster.width, raster.height):
        return raster
    from xp750_rip.precision import resize_grayscale_rows
    return resize_grayscale_rows(raster, width, height, method)


def _validate_request(request: ImageSizeRequest) -> None:
    numeric = (request.width_mm, request.height_mm, request.scale_percent)
    if any(value is not None and value <= 0 for value in numeric):
        raise ValueError("Физические размеры и scale-percent должны быть больше нуля")
    modes = sum(
        (
            request.width_mm is not None or request.height_mm is not None,
            request.scale_percent is not None,
            request.fit,
            request.native_size,
        )
    )
    if modes > 1:
        raise ValueError(
            "Выберите только один sizing mode: width/height, scale-percent, fit или native-size"
        )
    if request.center and (request.x_mm is not None or request.y_mm is not None):
        raise ValueError("--center нельзя сочетать с --x-mm/--y-mm")
    if request.x_mm is not None and request.x_mm < 0 or request.y_mm is not None and request.y_mm < 0:
        raise ValueError("Координаты placement не могут быть отрицательными")


def _calculate_windows_page_layout(
    raster: Grayscale8Raster,
    *,
    page: PpdPage,
    mode: PrinterMode,
    request: ImageSizeRequest,
    trace: PerformanceTrace | None,
) -> PageLayout:
    """Map an already composed Windows page onto the same physical sheet.

    XpsToTiff returns the complete physical page: application scaling,
    orientation, margins and placement are already pixels in ``raster``.
    The printer cannot address the PPD non-printable margins, so crop exactly
    that physical imageable rectangle and resample it to the backend canvas.
    This preserves every application coordinate instead of fitting/centring
    the complete page for a second time.
    """

    if page.width_points <= 0 or page.height_points <= 0:
        raise ValueError("PPD physical page dimensions must be positive")

    # PPD uses a bottom-left origin; Pillow/raster rows use a top-left origin.
    left = round(raster.width * page.imageable_left_points / page.width_points)
    right = round(raster.width * page.imageable_right_points / page.width_points)
    top = round(
        raster.height
        * (page.height_points - page.imageable_top_points)
        / page.height_points
    )
    bottom = round(
        raster.height
        * (page.height_points - page.imageable_bottom_points)
        / page.height_points
    )
    left = min(max(left, 0), raster.width - 1)
    right = min(max(right, left + 1), raster.width)
    top = min(max(top, 0), raster.height - 1)
    bottom = min(max(bottom, top + 1), raster.height)

    canvas_width_raw = int(page.printable_width_points * mode.horizontal_dpi / 72.0)
    canvas_width = canvas_width_raw - canvas_width_raw % 8
    canvas_height = int(page.printable_height_points * mode.vertical_dpi / 72.0)
    if canvas_width <= 0 or canvas_height <= 0:
        raise ValueError("PPD printable region не даёт положительного raster canvas")

    from xp750_rip.input.tiff_rows import CroppedRows
    cropped_image = type(raster)(right-left, CroppedRows(raster, left, top, right, bottom))
    if trace is not None:
        trace.event(
            "Windows composed page DATA",
            source_width=raster.width,
            source_height=raster.height,
            crop_left=left,
            crop_top=top,
            crop_right=right,
            crop_bottom=bottom,
            target_width=canvas_width,
            target_height=canvas_height,
            method=request.resampling.value,
        )
        with trace.stage(f"Windows page processing plan ({request.resampling.value})"):
            resized_image = _resize_image(
                cropped_image, (canvas_width, canvas_height), request.resampling,
            )
    else:
        resized_image = _resize_image(
            cropped_image, (canvas_width, canvas_height), request.resampling,
        )
    rendered = resized_image

    return PageLayout(
        page_name=page.name,
        source_width_pixels=raster.width,
        source_height_pixels=raster.height,
        rendered=rendered,
        rendered_width_mm=canvas_width / mode.horizontal_dpi * 25.4,
        rendered_height_mm=canvas_height / mode.vertical_dpi * 25.4,
        canvas_width_pixels=canvas_width,
        canvas_height_rows=canvas_height,
        placement_x_pixels=0,
        placement_y_rows=0,
        printable_width_mm=canvas_width / mode.horizontal_dpi * 25.4,
        printable_height_mm=canvas_height / mode.vertical_dpi * 25.4,
        orientation=LayoutOrientation.AUTO,
        resampling=request.resampling,
    )


def calculate_page_layout(
    raster: Grayscale8Raster,
    *,
    source_dpi: tuple[float, float] | None,
    page: PpdPage,
    mode: PrinterMode,
    request: ImageSizeRequest,
    trace: PerformanceTrace | None = None,
) -> PageLayout:
    """Calculate one explicit layout against a PPD printable region."""

    _validate_request(request)
    if request.windows_page:
        return _calculate_windows_page_layout(
            raster,
            page=page,
            mode=mode,
            request=request,
            trace=trace,
        )
    if trace is None:
        oriented, oriented_dpi = _rotate_for_orientation(
            raster, request.orientation, source_dpi
        )
    else:
        with trace.stage("orientation", requested=request.orientation.value):
            oriented, oriented_dpi = _rotate_for_orientation(
                raster, request.orientation, source_dpi
            )
    # The physical sheet remains portrait in the feed.  Landscape rotates the
    # image inside this canvas; it does not exchange the physical paper axes.
    printable_width_mm = page.printable_width_points * 25.4 / 72.0
    printable_height_mm = page.printable_height_points * 25.4 / 72.0
    source_aspect = oriented.width / oriented.height

    if request.width_mm is not None or request.height_mm is not None:
        if request.width_mm is not None and request.height_mm is not None:
            width_mm = min(request.width_mm, request.height_mm * source_aspect)
            height_mm = width_mm / source_aspect
        elif request.width_mm is not None:
            width_mm = request.width_mm
            height_mm = width_mm / source_aspect
        else:
            assert request.height_mm is not None
            height_mm = request.height_mm
            width_mm = height_mm * source_aspect
    elif request.native_size or request.scale_percent is not None:
        if oriented_dpi is None or oriented_dpi[0] <= 0 or oriented_dpi[1] <= 0:
            raise ValueError("native-size/scale-percent требует корректные TIFF XResolution/YResolution")
        scale = (request.scale_percent or 100.0) / 100.0
        width_mm = oriented.width / oriented_dpi[0] * 25.4 * scale
        height_mm = oriented.height / oriented_dpi[1] * 25.4 * scale
    else:
        # Safe product default is the same as an explicit --fit.
        scale = min(printable_width_mm / source_aspect, printable_height_mm)
        height_mm = scale
        width_mm = height_mm * source_aspect

    target_width = max(1, round(width_mm * mode.horizontal_dpi / 25.4))
    target_height = max(1, round(height_mm * mode.vertical_dpi / 25.4))
    canvas_width_raw = int(page.printable_width_points * mode.horizontal_dpi / 72.0)
    canvas_width = canvas_width_raw - canvas_width_raw % 8
    canvas_height = int(page.printable_height_points * mode.vertical_dpi / 72.0)
    if canvas_width <= 0 or canvas_height <= 0:
        raise ValueError("PPD printable region не даёт положительного raster canvas")
    automatic_fit = (
        request.fit
        or request.width_mm is None
        and request.height_mm is None
        and request.scale_percent is None
        and not request.native_size
    )
    if automatic_fit and (target_width > canvas_width or target_height > canvas_height):
        # PPD points -> mm -> pixels uses floating-point values, whereas canvas
        # dimensions must be integer/floor-aligned. Scale both axes together so
        # an explicit/default fit cannot exceed its own canvas by rounding.
        fit_scale = min(canvas_width / target_width, canvas_height / target_height)
        target_width = max(1, floor(target_width * fit_scale))
        target_height = max(1, floor(target_height * fit_scale))
    if target_width > canvas_width or target_height > canvas_height:
        raise ValueError(
            "Изображение выходит за printable region: "
            f"{target_width}x{target_height} px > {canvas_width}x{canvas_height} px"
        )

    if request.center:
        x = (canvas_width - target_width) // 2
        y = (canvas_height - target_height) // 2
    else:
        x = round((request.x_mm or 0.0) * mode.horizontal_dpi / 25.4)
        y = round((request.y_mm or 0.0) * mode.vertical_dpi / 25.4)
    if x + target_width > canvas_width or y + target_height > canvas_height:
        raise ValueError("Placement выходит за PPD printable region")

    if trace is None:
        rendered = resize_grayscale(
            oriented,
            width=target_width,
            height=target_height,
            method=request.resampling,
        )
    else:
        trace.event(
            "resample DATA",
            source_width=oriented.width,
            source_height=oriented.height,
            target_width=target_width,
            target_height=target_height,
            target_pixels=target_width * target_height,
            method=request.resampling.value,
        )
        with trace.stage(f"Image processing plan ({request.resampling.value})"):
            rendered = resize_grayscale(
                oriented,
                width=target_width,
                height=target_height,
                method=request.resampling,
            )
    return PageLayout(
        page_name=page.name,
        source_width_pixels=raster.width,
        source_height_pixels=raster.height,
        rendered=rendered,
        rendered_width_mm=target_width / mode.horizontal_dpi * 25.4,
        rendered_height_mm=target_height / mode.vertical_dpi * 25.4,
        canvas_width_pixels=canvas_width,
        canvas_height_rows=canvas_height,
        placement_x_pixels=x,
        placement_y_rows=y,
        printable_width_mm=canvas_width / mode.horizontal_dpi * 25.4,
        printable_height_mm=canvas_height / mode.vertical_dpi * 25.4,
        orientation=request.orientation,
        resampling=request.resampling,
    )
