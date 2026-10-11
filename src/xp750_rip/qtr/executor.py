# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Execution of already-compiled QTR curve tables.

Profile authoring operations (limits, partitions, crossover and linearization)
are intentionally absent: ``quadprofile`` has already baked them into the 256
samples of every curve.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from xp750_rip.qtr.quad import QTR_UINT16_MAX, QuadProfile


class UnsupportedQtrSemanticsError(ValueError):
    """Raised when evaluation would require undocumented QTR interpolation."""


@dataclass(frozen=True, slots=True)
class LogicalInkAmount:
    """Continuous-tone amount for one QTR logical ink."""

    ink_code: str
    raw_uint16: int

    @property
    def normalized(self) -> float:
        return self.raw_uint16 / QTR_UINT16_MAX


@dataclass(frozen=True, slots=True)
class QtrExecutionResult:
    """Logical output at one exact 8-bit QTR tonal-scale code."""

    input_code: int
    amounts: tuple[LogicalInkAmount, ...]

    @property
    def input_tone(self) -> float:
        """QTR tonal demand: 0 is paper white and 1 is maximum image black."""

        return self.input_code / 255

    @property
    def raw_by_ink(self) -> dict[str, int]:
        return {amount.ink_code: amount.raw_uint16 for amount in self.amounts}

    @property
    def normalized_by_ink(self) -> dict[str, float]:
        return {amount.ink_code: amount.normalized for amount in self.amounts}

    @property
    def active_inks(self) -> tuple[str, ...]:
        return tuple(amount.ink_code for amount in self.amounts if amount.raw_uint16 > 0)

    @property
    def total_raw_output(self) -> int:
        """Diagnostic sum only; it is not a calibrated physical ink-load metric."""

        return sum(amount.raw_uint16 for amount in self.amounts)


def evaluate_profile_code(input_code: int, profile: QuadProfile) -> QtrExecutionResult:
    """Evaluate every final curve at one exact documented 8-bit table index."""

    if isinstance(input_code, bool) or not isinstance(input_code, int):
        raise TypeError("QTR input code must be an integer")
    if input_code < 0 or input_code > 255:
        raise ValueError("QTR input code must be in the range 0..255")
    return QtrExecutionResult(
        input_code=input_code,
        amounts=tuple(
            LogicalInkAmount(curve.ink_code, curve.samples[input_code]) for curve in profile.curves
        ),
    )


def evaluate_profile(input_tone: float, profile: QuadProfile) -> QtrExecutionResult:
    """Evaluate a normalized tone only when it maps exactly to the 8-bit domain.

    The inspected QTR specification defines 256 table entries but does not define
    interpolation between them.  Phase 6 therefore rejects off-grid inputs rather
    than silently choosing nearest, linear, spline, or cubic interpolation.
    """

    if isinstance(input_tone, bool) or not isinstance(input_tone, (int, float)):
        raise TypeError("normalized QTR input tone must be numeric")
    value = float(input_tone)
    if not math.isfinite(value) or value < 0.0 or value > 1.0:
        raise ValueError("normalized QTR input tone must be finite and in 0..1")
    scaled = value * 255
    input_code = round(scaled)
    if not math.isclose(scaled, input_code, rel_tol=0.0, abs_tol=1e-9):
        raise UnsupportedQtrSemanticsError(
            "QTR interpolation between the documented 256 samples is UNKNOWN; "
            "supply an exact n/255 tone"
        )
    return evaluate_profile_code(input_code, profile)
