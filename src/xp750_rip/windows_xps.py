# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""High-precision Windows XPS adapter, run by the existing desktop RIP process.

The system rasterizer produces premultiplied linear scRGB float32. Cython
composites on white and encodes the existing QUAD tone domain without an 8-bit
intermediate. Output strips and input rectangles have bounded working sizes.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from shutil import disk_usage
from xml.etree import ElementTree as ET

import imagecodecs
import numpy as np
import tifffile

PSF = "http://schemas.microsoft.com/windows/2003/08/printing/printschemaframework"
PSK = "http://schemas.microsoft.com/windows/2003/08/printing/printschemakeywords"
BAND_BUDGET = 16 * 1024 * 1024


def _ticket(data):
    """Keep feature options/properties; merge scopes before selecting geometry."""
    if not data:
        return {}
    root = ET.fromstring(data)
    if root.tag != f"{{{PSF}}}PrintTicket":
        raise ValueError("Expected a PrintTicket document")
    parameters = {}
    for initializer in root.findall(f"{{{PSF}}}ParameterInit"):
        value = initializer.find(f"{{{PSF}}}Value")
        if value is not None and value.text:
            parameters[initializer.get("name", "").split(":")[-1]] = value.text
    result = {}
    for feature in root.findall(f"{{{PSF}}}Feature"):
        name = feature.get("name", "").split(":")[-1]
        option = feature.find(f"{{{PSF}}}Option")
        if option is None:
            continue
        props = {}
        for node in option.iter():
            if node.tag in (f"{{{PSF}}}ScoredProperty", f"{{{PSF}}}Property"):
                value = node.find(f"{{{PSF}}}Value")
                text = value.text if value is not None else None
                reference = node.find(f"{{{PSF}}}ParameterRef")
                if text is None and reference is not None:
                    text = parameters.get(reference.get("name", "").split(":")[-1])
                if text is not None:
                    props[node.get("name", "").split(":")[-1]] = text
        result[name] = (option.get("name", "").split(":")[-1], props)
    return result


def _option(features, name, default):
    return features.get(name, (default, {}))[0]


def _merge_features(parent, child):
    for name, (option, props) in child.items():
        old_option, old_props = parent.get(name, (None, {}))
        parent[name] = (option, {**old_props, **props} if option == old_option else props)


def _validate_host_features(features):
    for name, (option, props) in features.items():
        if "NUp" in name:
            count = props.get("PagesPerSheet")
            if (count is not None and int(count) != 1) or (
                count is None and option not in ("None", "Identity", "OneUp")
            ):
                raise ValueError("High-precision XPS rendering does not support host N-up")
        if "Duplex" in name and option not in ("OneSided", "None"):
            raise ValueError("High-precision XPS rendering requires a one-sided ticket")
        if name == "PageScaling" and option not in ("None", "Identity"):
            raise ValueError(
                "High-precision XPS rendering requires application-composed page scaling"
            )
        if name == "PageForceFrontSide" and option not in ("None", "False"):
            raise ValueError("Front-side blank insertion requires a supported duplex workflow")


@dataclass(frozen=True)
class PagePlan:
    source_page: int
    width: int
    height: int
    rotation: int
    dpi: tuple[float, float]
    physical_size_um: tuple[int, int] | None = None


def _plan(index, width, height, features):
    _validate_host_features(features)
    option = _option(features, "PageOrientation", "Portrait")
    try:
        rotation = {"Portrait": 0, "Landscape": 1, "ReversePortrait": 2, "ReverseLandscape": 3}[
            option
        ]
    except KeyError as error:
        raise ValueError(f"Unsupported XPS orientation: {option}") from error
    resolution = features.get("PageResolution", ("", {}))[1]
    dpi = (float(resolution.get("ResolutionX", "600")), float(resolution.get("ResolutionY", "600")))
    if any(not math.isfinite(v) or not 0 < v <= 9600 for v in dpi):
        raise ValueError("Invalid XPS rasterization DPI")
    if not all(math.isfinite(v) and v > 0 for v in (width, height)):
        raise ValueError("Invalid XPS page dimensions")
    media = features.get("PageMediaSize", ("", {}))[1]
    # The native printer's sheet axes remain portrait-fed. PageOrientation
    # rotates application content into those axes, exactly once.
    if "MediaSizeWidth" in media and "MediaSizeHeight" in media:
        width, height = (
            float(media["MediaSizeWidth"]) * 96 / 25400,
            float(media["MediaSizeHeight"]) * 96 / 25400,
        )
    elif rotation % 2:
        width, height = height, width
    # Windows' TIFF adapter truncates physical dimensions to whole pixels.
    output = (int(width * dpi[0] / 96), int(height * dpi[1] / 96))
    if any(v <= 0 or v > 2**31 - 1 for v in output):
        raise ValueError("XPS raster dimensions exceed the native coordinate range")
    physical = (round(width * 25400 / 96), round(height * 25400 / 96))
    return PagePlan(index, *output, rotation, dpi, physical)


def _float_band(native, handle, plan, start, end, cancel_check=None):
    """Map the output rectangle back to page coordinates without a page array."""
    width, height, k = plan.width, plan.height, plan.rotation
    rows = end - start
    if k % 2:
        # For CCW rotation, output[y,x] = input[x, source_width-1-y].
        raw = np.empty((width, rows, 4), dtype=np.float32)
        x = height - end if k == 1 else start
        native.render_rect(handle, x, 0, rows, width, raw, cancel_check)
    else:
        raw = np.empty((rows, width, 4), dtype=np.float32)
        native.render_rect(
            handle, 0, height - end if k == 2 else start, width, rows, raw, cancel_check
        )
    return np.ascontiguousarray(np.rot90(raw, k)) if k else raw


def render_xps16(
    source: Path,
    ticket: Path,
    output: Path,
    *,
    rgb=False,
    trace=None,
    cancel_check=None,
    native=None,
):
    """Render one TIFF page per XPS page; copies remain the RIP's responsibility.

    No fallback to XpsToTiff: unsupported system activation or a missing native
    extension is an explicit failure. A partial file is never a ready artifact.
    """
    if native is None:
        try:
            from xp750_rip import _windows_xps_native as native
        except ImportError as error:
            raise RuntimeError(
                "Windows high-precision XPS adapter is unavailable; rebuild the complete runtime"
            ) from error
    from xp750_rip._cython_kernels import scrgb_to_uint16_kernel

    if output.exists():
        raise FileExistsError(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(output.name + ".partial")
    features = _ticket(ticket.read_bytes())
    handle = native.open(str(source.resolve()))
    clipped = [0, 0]
    created = False
    pages = []
    sequence_defaults = {}
    label = "Windows XPS float32 rasterization to " + ("RGB16" if rgb else "Gray16")
    if trace:
        trace.event(label + " START")
    try:
        for index in range(native.page_count(handle)):
            if cancel_check:
                cancel_check()
            width, height, *tickets = native.page_info(handle, index)
            effective = _ticket(tickets[0]) if tickets else {}
            if index == 0:
                sequence_defaults = dict(effective)
            _merge_features(effective, features)
            for scoped in tickets[1:]:
                # Page scopes override feature selections, but never repeat
                # copies or reapply the original application's layout.
                _merge_features(effective, _ticket(scoped))
            pages.append(_plan(index, width, height, effective))
        order = _option({**sequence_defaults, **features}, "JobPageOrder", "Standard")
        if order not in ("Standard", "Reverse"):
            raise ValueError(f"Unsupported document page order: {order}")
        if order == "Reverse":
            pages.reverse()
        # Native queues have one selected paper. Fail clearly rather than
        # stretching per-page media silently into a different backend canvas.
        # Compare declared physical sizes before pixel quantization. At 96 dpi
        # truncating one pixel can otherwise look like a 0.26 mm media change.
        dimensions = [p.physical_size_um for p in pages]
        if any(
            any(abs(a - b) > 2 for a, b in zip(size, dimensions[0], strict=True))
            for size in dimensions
        ):
            raise ValueError("Mixed page media sizes require separate virtual print jobs")
        selected_media = features.get("PageMediaSize", ("", {}))[1]
        if "MediaSizeWidth" in selected_media and "MediaSizeHeight" in selected_media:
            selected_dimensions = (
                round(float(selected_media["MediaSizeWidth"])),
                round(float(selected_media["MediaSizeHeight"])),
            )
            if any(
                any(abs(a - b) > 2 for a, b in zip(size, selected_dimensions, strict=True))
                for size in dimensions
            ):
                raise ValueError(
                    "Page media override differs from the virtual printer's selected sheet"
                )
        destination = partial.open("xb")
        created = True
        with destination, tifffile.TiffWriter(destination, bigtiff=True) as writer:
            for number, plan in enumerate(pages):
                if cancel_check:
                    cancel_check()
                native.begin_page(
                    handle,
                    plan.source_page,
                    *(plan.dpi[::-1] if plan.rotation % 2 else plan.dpi),
                )
                rows = max(1, min(128, BAND_BUDGET // (plan.width * 32)))
                if trace:
                    trace.event(
                        f"XPS page {number + 1} float32 bands START",
                        width=plan.width,
                        height=plan.height,
                        band_rows=rows,
                        dpi=plan.dpi,
                        source_page=plan.source_page,
                    )

                def strips(plan=plan, rows=rows):
                    for start in range(0, plan.height, rows):
                        if cancel_check:
                            cancel_check()
                        if (
                            disk_usage(output.parent).free
                            < 64 * 1024 * 1024 + plan.width * rows * 6
                        ):
                            raise OSError(
                                "Insufficient disk space for high-precision TIFF; preserving 64 MiB reserve"
                            )
                        try:
                            pixels = _float_band(
                                native,
                                handle,
                                plan,
                                start,
                                min(start + rows, plan.height),
                                cancel_check,
                            )
                        except OSError:
                            if cancel_check:
                                cancel_check()
                            raise
                        values, low, high = scrgb_to_uint16_kernel(pixels, rgb)
                        clipped[0] += low
                        clipped[1] += high
                        # tifffile interprets ndarray iterators as whole
                        # pages. Encoded byte chunks explicitly mean strips.
                        strip = values if rgb else np.ascontiguousarray(values[:, :, 0])
                        yield imagecodecs.lzw_encode(strip)

                writer.write(
                    strips(),
                    shape=(plan.height, plan.width, 3) if rgb else (plan.height, plan.width),
                    dtype=np.uint16,
                    photometric="rgb" if rgb else "minisblack",
                    rowsperstrip=rows,
                    compression="lzw",
                    maxworkers=1,
                    resolution=plan.dpi,
                    resolutionunit="INCH",
                    metadata=None,
                    description=json.dumps(
                        {
                            "pytone_rip_renderer": "windows-xps-float32-v1",
                            "tone_domain": "encoded-sRGB-299-587-114",
                            "alpha": "linear-white-composite",
                            "range_policy": "fixed-SDR-white-1; final-device-clip; no-adaptive-tone-map",
                            "xps_source_page": plan.source_page,
                            "xps_input_precision": "varies by embedded resource and drawing",
                        }
                    ),
                )
        if cancel_check:
            cancel_check()
        partial.rename(output)
        if trace:
            trace.event(
                label + " END",
                pages=len(pages),
                below_range=clipped[0],
                above_range=clipped[1],
                input_precision="XPS/OXPS; varies by resource",
            )
        return {
            "renderer": "windows-xps-float32-v1",
            "pages": len(pages),
            "below_range": clipped[0],
            "above_range": clipped[1],
        }
    except BaseException:
        if created:
            partial.unlink(missing_ok=True)
        raise
    finally:
        native.close(handle)
