# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only
"""A 16-bit tonal target, rendered a scanline at a time with an embedded font."""

from __future__ import annotations

import json
import math
import re
from datetime import datetime
from functools import lru_cache
from pathlib import Path

import numpy as np


@lru_cache(maxsize=1)
def _outlines():
    glyphs = json.loads((Path(__file__).parent / "data/tonal_test_font.json").read_text(encoding="utf-8"))["glyphs"]
    result = {}
    for character, path in glyphs.items():
        tokens = re.findall(r"[MLQ]|-?\d+", path)
        edges, x, y, pos = [], 0.0, 0.0, 0
        while pos < len(tokens):
            op = tokens[pos]
            a, b = float(tokens[pos + 1]), float(tokens[pos + 2])
            pos += 3
            if op == "M":
                x, y = a, b
            elif op == "L":
                edges.append((x, y, a, b))
                x, y = a, b
            else:
                c, d = float(tokens[pos]), float(tokens[pos + 1])
                pos += 2
                px, py = x, y
                for k in range(1, 13):
                    t = k / 12.0
                    s = 1 - t
                    nx, ny = s*s*x + 2*s*t*a + t*t*c, s*s*y + 2*s*t*b + t*t*d
                    edges.append((px, py, nx, ny))
                    px, py = nx, ny
                x, y = c, d
        result[character] = edges
    return result


@lru_cache(maxsize=512)
def _glyph(character: str, height: int):
    width = math.ceil(height * 16 / 24)
    yy, xx = np.mgrid[:height, :width]
    gx, gy = (xx + .5) * 16 / width, (yy + .5) * 24 / height
    distance = np.full((height, width), 1e30)
    for x0, y0, x1, y1 in _outlines().get(character, []):
        dx, dy = x1 - x0, y1 - y0
        den = dx*dx + dy*dy
        t = np.clip(((gx-x0)*dx + (gy-y0)*dy) / den, 0, 1) if den else 0
        distance = np.minimum(distance, (gx-x0-t*dx)**2 + (gy-y0-t*dy)**2)
    return np.floor(np.clip(.5 + (.8 - np.sqrt(distance))*height/24, 0, 1)*65535 + .5).astype(np.uint16)


class TonalTestPage:
    """190 mm wide content intended for a centered portrait A4 sheet."""

    def __init__(self, printer_label: str, *, day: str | None = None, dpi: int = 360):
        if dpi not in (360, 720):
            raise ValueError("Tonal target DPI must be 360 or 720")
        self.dpi = dpi
        self.shapes = []
        self._header_y = 0.0
        self._header("pyToneRIP - Tonal Test", 11)
        label = "".join(c if 32 <= ord(c) <= 126 else "?" for c in printer_label)[:128]
        self._header(label + (" | " if label else "") + (day or datetime.now().astimezone().date().isoformat()), 8)
        self._header(f"16-bit grayscale | {dpi} DPI", 8)
        self.body_y = self._header_y + 1.5
        self._shape(0, 0, 186, 14, "x", 1, 0)
        self._shape(0, 18, 186, 14, "y", 0, 1)
        for y, black in ((40, 1), (119, 0)):
            self._shape(6, y, 72, 72, "radial", black, 0)
            self._shape(108, y, 72, 72, "radial", black, 21)
        self._text(8, 114 + self.body_y, "Continuous", 8)
        self._text(111, 114 + self.body_y, "21 rings", 8)
        for count, y in ((21, 205), (51, 243)):
            for j in range(count):
                self._shape(j*186/count, y, 186/count, 12, "rect", 1-j/(count-1), 0)
        self._shape(0, 224, 186, 12, "x", 1, 0)
        for j in range(11):
            for y in (218, 256):
                self._text(j*18.3, y+self.body_y, str(10-j), 7.5)
        self.width_mm = 190.0
        self.height_mm = 256 + 7.5*25.4/72 + self.body_y + 4
        self.width = math.floor(self.width_mm*dpi/25.4 + .5)
        self.height = math.floor(self.height_mm*dpi/25.4 + .5)
        self._x = (np.arange(self.width) + .5)*25.4/dpi - 2

    def _text(self, x, y, text, points):
        h = points*25.4/72
        self.shapes.append((x, y, len(text)*h*16/24, h, "text", points, text))

    def _header(self, text, points):
        capacity = max(10, math.floor(186 / (points*25.4/72*16/24)))
        while text:
            n = min(len(text), capacity)
            if n < len(text):
                split = text.rfind(" ", 0, n+1)
                if split > 0:
                    n = split
            self._text(0, self._header_y, text[:n], points)
            self._header_y += points*25.4/72 + .5
            text = text[n:].lstrip(" ")

    def _shape(self, x, y, w, h, kind, a, b):
        self.shapes.append((x, y+self.body_y, w, h, kind, a, b))

    def row(self, y: int):
        if not 0 <= y < self.height:
            raise ValueError("Tonal target row is out of bounds")
        my = (y+.5)*25.4/self.dpi - 2
        row = np.full(self.width, 65535, dtype=np.uint16)
        for sx, sy, w, h, kind, a, b in self.shapes:
            if not sy <= my < sy+h:
                continue
            x0 = max(0, math.ceil((sx+2)*self.dpi/25.4-.5))
            x1 = min(self.width, math.ceil((sx+w+2)*self.dpi/25.4-.5))
            u, v = (self._x[x0:x1]-sx)/w, (my-sy)/h
            if kind == "text":
                advance = h*16/24
                indices = np.floor((self._x[x0:x1]-sx)/advance).astype(int)
                gh = math.ceil(a*self.dpi/72)
                gw = math.ceil(gh*16/24)
                px = np.floor(((self._x[x0:x1]-sx)-indices*advance)/advance*gw).astype(int)
                py = math.floor(v*gh)
                for index in np.unique(indices):
                    if not 0 <= index < len(b):
                        continue
                    mask = (indices == index) & (px >= 0) & (px < gw)
                    alpha = _glyph(b[index], gh)[py, px[mask]].astype(float)/65535
                    positions = np.flatnonzero(mask) + x0
                    row[positions] = np.floor(row[positions]*(1-alpha)+.5).astype(np.uint16)
                continue
            if kind == "rect":
                ink = np.full(len(u), a)
            elif kind == "x":
                ink = a + (b-a)*u
            elif kind == "y":
                ink = np.full(len(u), a+(b-a)*v)
            else:
                radius = np.sqrt((u-.5)**2 + (v-.5)**2)*2
                mask = radius <= 1
                if b:
                    radius = np.minimum(b-1, np.floor(radius*b))/(b-1)
                ink = 1-radius if a else radius
                positions = np.flatnonzero(mask) + x0
                row[positions] = np.floor((1-ink[mask])*65535+.5).astype(np.uint16)
                continue
            row[x0:x1] = np.floor((1-ink)*65535+.5).astype(np.uint16)
        return row

    def write_tiff(self, path: Path):
        import imagecodecs
        import tifffile

        path.parent.mkdir(parents=True, exist_ok=True)
        def strips():
            for y in range(0, self.height, 16):
                strip = np.stack([self.row(i) for i in range(y, min(y+16, self.height))])
                delta = strip.copy()
                delta[:, 1:] = strip[:, 1:] - strip[:, :-1]
                yield imagecodecs.lzw_encode(delta.astype("<u2").tobytes())
        tifffile.imwrite(path, data=strips(), shape=(self.height, self.width), dtype="uint16",
                         photometric="minisblack", compression="lzw", predictor=True, rowsperstrip=16,
                         resolution=(self.dpi, self.dpi), resolutionunit="INCH", metadata=None,
                         software="pyToneRIP", description="16-bit grayscale tonal test")


def render_tonal_test_job(request, output_dir: Path, *, model=None):
    """Use the ordinary profiled RIP, with a fixed portrait A4 target layout."""
    from xp750_rip.application import HalftoneConfiguration, MonoRipService, PrintJobConfiguration
    from xp750_rip.contone import LogicalChannelId
    from xp750_rip.direction import parse_print_direction
    from xp750_rip.layout import ImageSizeRequest, LayoutOrientation
    from xp750_rip.printers import default_registry
    from xp750_rip.printers.capabilities import PrinterInstance
    from xp750_rip.qtr.bundle import load_profile_bundle
    from xp750_rip.qtr.runtime_controls import (
        QtrAdvancedAdjustments,
        QtrBlendConfiguration,
        QtrRuntimeProfileSet,
        QtrToneBlendWeights,
    )
    from xp750_rip.raster import PhysicalPathId
    from xp750_rip.routing import ChannelRouter

    raw = request.get("configuration_json")
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("The tonal test page requires the selected curve profiles and print settings")
    cfg = json.loads(raw)
    if cfg.get("schema") not in ("pytone-rip.configuration", "pytone-rip.rip-configuration") or cfg.get("version") != 1:
        raise ValueError("Invalid tonal test print configuration")
    printer, profiles, adjustments, channels = (cfg[key] for key in ("printer", "profiles", "adjustments", "channels"))
    if printer["model"] != request["model"]:
        raise ValueError("The tonal test printer differs from the maintenance target")
    model = model or default_registry().get(request["model"])
    model.page("A4")  # Fail before generating anything if A4 is unavailable.
    curves = profiles["curves"]
    if not isinstance(curves, list) or not 1 <= len(curves) <= 3 or not all(curves):
        raise ValueError("Select one to three curve profiles before printing the tonal test page")
    bundles = tuple(load_profile_bundle(quad_path=Path(path)) for path in curves)
    blend = tuple(profiles["blend"])
    split = bool(profiles["split_tone"])
    weights = QtrToneBlendWeights(*(tuple(profiles[key]) if split else blend
                                  for key in ("highlights", "midtones", "shadows")))
    runtime = QtrRuntimeProfileSet(tuple(bundle.quad for bundle in bundles),
                                  QtrBlendConfiguration(split, weights),
                                  QtrAdvancedAdjustments(adjustments["gamma"], adjustments["ink_limit"]))
    output_dir.mkdir(parents=True, exist_ok=True)
    image_path = output_dir / "last-maintenance-tonal-test.tiff"
    TonalTestPage(model.display_name).write_tiff(image_path)
    job = PrintJobConfiguration(
        source_image=image_path, profile_bundle=bundles[0], runtime_profile_bundles=bundles,
        qtr_runtime=runtime, target=PrinterInstance(model, model.display_name),
        mode=model.mode(printer["mode"]), media_id=printer["media"], page_id="A4",
        input_source_id=printer["input_source"], direction=parse_print_direction(printer["direction"]),
        image_size=ImageSizeRequest(native_size=True, orientation=LayoutOrientation.PORTRAIT, center=True),
        active_logical_inks=tuple(LogicalChannelId(ink) for ink in channels["logical_inks"]),
        router=ChannelRouter({LogicalChannelId(ink): PhysicalPathId(path) for ink, path in channels["routing"].items()}),
        inkset={}, halftone=HalftoneConfiguration(algorithm=adjustments["dither"]),
        device_settings=tuple(sorted((printer.get("device_settings") or {}).items())),
        ink_configuration=printer.get("ink_configuration"), processing_bits=16,
        allow_unsupported_ppd_combinations=printer.get("allow_unsupported_ppd_combinations", False),
    )
    rendered = MonoRipService().render(job)
    if not rendered.validation.passed:
        raise ValueError("Generated tonal test page failed native validation")
    path = output_dir / "last-maintenance-test-page.prn"
    path.write_bytes(rendered.data)
    return rendered.data, path
