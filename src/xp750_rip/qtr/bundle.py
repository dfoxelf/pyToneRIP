# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Printer-independent bundle for direct QTR profile inputs."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from os import fstat
from pathlib import Path
from typing import TYPE_CHECKING

from xp750_rip.qtr.quad import QuadProfile, parse_quad

if TYPE_CHECKING:
    from xp750_rip.performance import PerformanceTrace


def _read_fresh_snapshot(path: Path) -> tuple[bytes, int, int]:
    """Read one uncached, internally consistent profile snapshot for this job."""

    with path.open("rb") as stream:
        before = fstat(stream.fileno())
        data = stream.read()
        after = fstat(stream.fileno())
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise OSError(f"QUAD changed while it was being read: {path}")
    if len(data) != after.st_size:
        raise OSError(f"QUAD could not be read completely: {path}")
    return data, after.st_size, after.st_mtime_ns


def _decode_profile_text(source: bytes) -> str:
    """Match ``Path.read_text`` universal-newline semantics from one byte snapshot."""

    return source.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")


@dataclass(frozen=True, slots=True)
class QtrProfileBundle:
    quad: QuadProfile
    quad_path: Path
    quad_sha256: str
    source_size_bytes: int
    source_modified_ns: int
    warnings: tuple[str, ...]

    @property
    def logical_inks(self) -> tuple[str, ...]:
        return self.quad.ink_codes


def load_profile_bundle(
    *,
    quad_path: Path,
    trace: PerformanceTrace | None = None,
) -> QtrProfileBundle:
    if not quad_path.is_file():
        raise ValueError(f"QUAD не найден: {quad_path}")

    if trace is None:
        source, source_size, source_modified_ns = _read_fresh_snapshot(quad_path)
        quad = parse_quad(_decode_profile_text(source))
        quad_digest = sha256(source).hexdigest()
    else:
        with trace.stage("QUAD fresh file snapshot", path=quad_path):
            source, source_size, source_modified_ns = _read_fresh_snapshot(quad_path)
        with trace.stage("QUAD parsing", path=quad_path):
            quad = parse_quad(_decode_profile_text(source))
        with trace.stage("SHA-256 computation (profiles)"):
            quad_digest = sha256(source).hexdigest()
        trace.event(
            "QUAD snapshot DATA",
            path=quad_path,
            size_bytes=source_size,
            modified_ns=source_modified_ns,
            sha256=quad_digest,
            cache="none",
        )
    return QtrProfileBundle(
        quad=quad,
        quad_path=quad_path.resolve(),
        quad_sha256=quad_digest,
        source_size_bytes=source_size,
        source_modified_ns=source_modified_ns,
        warnings=(),
    )
