# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Preservation-first parser for compiled ``.quad`` calibration tables.

A compiled QUAD file contains one 256-entry unsigned 16-bit table per logical
ink.  Curve marker comments identify the tables in the supplied real profile.
This layer deliberately has no knowledge of printer selectors or PhysicalPath.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# The producing application's name is metadata; the RIP suffix identifies the
# ink-declaration header. Preserve its original bytes when serializing.
_HEADER_RE = re.compile(r"^##\s*\S*RIP\s+(.+?)\s*$", re.IGNORECASE)
_VERSION_RE = re.compile(r"^#\s*QuadProfile\s+Version\s+(.+?)\s*$", re.IGNORECASE)
_CURVE_RE = re.compile(r"^#\s*([^#\s]+)\s+curve\s*$", re.IGNORECASE)
_INTEGER_RE = re.compile(r"^[0-9]+$")

QTR_CURVE_SAMPLE_COUNT = 256
QTR_UINT16_MAX = 65535


@dataclass(frozen=True, slots=True)
class QuadSourceLine:
    """One original line, retained in source order for lossless preservation."""

    line_number: int
    raw: str
    kind: str
    curve_ink: str | None = None


@dataclass(frozen=True, slots=True)
class QuadCurve:
    """One compiled logical-ink table in source order."""

    ink_code: str
    samples: tuple[int, ...]
    marker_line_number: int
    raw_sample_lines: tuple[str, ...]

    def __post_init__(self) -> None:
        if len(self.samples) != QTR_CURVE_SAMPLE_COUNT:
            raise ValueError(
                f"curve {self.ink_code!r} must contain exactly "
                f"{QTR_CURVE_SAMPLE_COUNT} samples, got {len(self.samples)}"
            )
        if any(sample < 0 or sample > QTR_UINT16_MAX for sample in self.samples):
            raise ValueError(f"curve {self.ink_code!r} contains a value outside uint16")


@dataclass(frozen=True, slots=True)
class QuadProfile:
    """Parsed compiled profile with exact original text and typed curve access."""

    source_text: str
    lines: tuple[QuadSourceLine, ...]
    curves: tuple[QuadCurve, ...]
    declared_inks: tuple[str, ...] | None
    quadprofile_version: str | None

    def __post_init__(self) -> None:
        if not self.curves:
            raise ValueError("compiled QUAD profile must contain at least one curve")
        codes = tuple(curve.ink_code for curve in self.curves)
        if len(set(codes)) != len(codes):
            raise ValueError("compiled QUAD profile contains duplicate curve markers")
        if self.declared_inks is not None and self.declared_inks != codes:
            raise ValueError(
                "QUAD header ink order does not match curve marker order: "
                f"{self.declared_inks!r} != {codes!r}"
            )

    @property
    def ink_codes(self) -> tuple[str, ...]:
        return tuple(curve.ink_code for curve in self.curves)

    def curve(self, ink_code: str) -> QuadCurve:
        for curve in self.curves:
            if curve.ink_code == ink_code:
                return curve
        raise KeyError(ink_code)

    def serialize_preserved(self) -> str:
        """Return the exact text supplied to the parser."""

        return self.source_text


def parse_quad(text: str) -> QuadProfile:
    """Parse a compiled ``.quad`` file without discarding any source line."""

    declared_inks: tuple[str, ...] | None = None
    compact_header = False
    version: str | None = None
    source_lines: list[QuadSourceLine] = []
    curves: list[QuadCurve] = []
    current_ink: str | None = None
    current_marker = 0
    current_samples: list[int] = []
    current_raw_samples: list[str] = []

    def finish_curve() -> None:
        nonlocal current_ink, current_marker, current_samples, current_raw_samples
        if current_ink is None:
            return
        curves.append(
            QuadCurve(
                ink_code=current_ink,
                samples=tuple(current_samples),
                marker_line_number=current_marker,
                raw_sample_lines=tuple(current_raw_samples),
            )
        )
        current_ink = None
        current_marker = 0
        current_samples = []
        current_raw_samples = []

    for line_number, raw in enumerate(text.splitlines(keepends=True), start=1):
        body = raw.rstrip("\r\n")
        semantic_body = body.lstrip("\ufeff") if line_number == 1 else body
        stripped = semantic_body.strip()
        header_match = _HEADER_RE.match(stripped)
        version_match = _VERSION_RE.match(stripped)
        curve_match = _CURVE_RE.match(stripped)

        if curve_match:
            finish_curve()
            current_ink = curve_match.group(1)
            current_marker = line_number
            source_lines.append(QuadSourceLine(line_number, raw, "curve_marker", current_ink))
        elif header_match:
            if declared_inks is not None:
                raise ValueError(f"line {line_number}: duplicate QUAD header")
            header_value = header_match.group(1).strip()
            compact_header = "," not in header_value
            if compact_header:
                if not re.fullmatch(r"[A-Za-z0-9]+", header_value):
                    raise ValueError(
                        f"line {line_number}: malformed compact QUAD ink header"
                    )
                # Older compiled profiles encode one single-character logical
                # ink identity per header character and omit per-table markers.
                # Case is identity-bearing (for example KCMYcmk).
                declared_inks = tuple(header_value)
            else:
                declared_inks = tuple(item.strip() for item in header_value.split(","))
            if not declared_inks or any(not item for item in declared_inks):
                raise ValueError(f"line {line_number}: malformed QUAD ink header")
            source_lines.append(QuadSourceLine(line_number, raw, "header"))
        elif version_match:
            if version is not None:
                raise ValueError(f"line {line_number}: duplicate QuadProfile version")
            version = version_match.group(1)
            source_lines.append(QuadSourceLine(line_number, raw, "version"))
        elif not stripped:
            source_lines.append(QuadSourceLine(line_number, raw, "blank", current_ink))
        elif stripped.startswith(("#", '"#')):
            source_lines.append(QuadSourceLine(line_number, raw, "comment", current_ink))
        else:
            if current_ink is None:
                if compact_header and declared_inks is not None and _INTEGER_RE.fullmatch(stripped):
                    if len(curves) >= len(declared_inks):
                        raise ValueError(
                            f"line {line_number}: compact QUAD contains more tables than its header"
                        )
                    current_ink = declared_inks[len(curves)]
                    current_marker = line_number
                else:
                    raise ValueError(
                        f"line {line_number}: numeric sample appears before a curve marker"
                    )
            if not _INTEGER_RE.fullmatch(stripped):
                raise ValueError(f"line {line_number}: malformed QUAD sample {stripped!r}")
            value = int(stripped)
            if value > QTR_UINT16_MAX:
                raise ValueError(f"line {line_number}: QUAD sample {value} exceeds uint16")
            current_samples.append(value)
            current_raw_samples.append(raw)
            source_lines.append(QuadSourceLine(line_number, raw, "sample", current_ink))
            if compact_header and len(current_samples) == QTR_CURVE_SAMPLE_COUNT:
                finish_curve()

    finish_curve()
    return QuadProfile(
        source_text=text,
        lines=tuple(source_lines),
        curves=tuple(curves),
        declared_inks=declared_inks,
        quadprofile_version=version,
    )
