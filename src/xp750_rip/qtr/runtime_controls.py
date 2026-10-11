# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Transparent EpsonRIP-defined runtime controls for compiled QTR profiles.

This module deliberately does not claim byte-compatible reproduction of the
closed QuadToneRIP runtime.  It preserves the user-facing model (one to three
complete profiles, optional H/M/S split tone, gamma and ink limit) while making
every numerical rule explicit and testable.
"""

from __future__ import annotations

from array import array
from dataclasses import dataclass
from functools import cache
from math import floor, log, pow

import numpy as np

from xp750_rip.contone import LogicalChannelId, LogicalContonePlane
from xp750_rip.qtr.quad import QTR_UINT16_MAX, QuadProfile
from xp750_rip.qtr.raster_executor import execute_curve_codes

QTR_HIGHLIGHT_CODE = 0
QTR_MIDTONE_CODE = 128
QTR_SHADOW_CODE = 255
QTR_GAMMA_MIN = -15
QTR_GAMMA_MAX = 15
QTR_INK_LIMIT_MIN = -50
QTR_INK_LIMIT_MAX = 50


def _validate_weights(name: str, values: tuple[int, ...], count: int) -> None:
    if len(values) != count:
        raise ValueError(f"{name}: требуется {count} weights, получено {len(values)}")
    if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
        raise TypeError(f"{name}: weights должны быть целыми")
    if any(value < 0 or value > 100 for value in values):
        raise ValueError(f"{name}: каждый weight должен быть в диапазоне 0..100")
    if sum(values) != 100:
        raise ValueError(f"{name}: active profile weights должны суммироваться ровно в 100")


@dataclass(frozen=True, slots=True)
class QtrToneBlendWeights:
    """Contribution percentages at QTR codes 0, 128 and 255."""

    highlights: tuple[int, ...]
    midtones: tuple[int, ...]
    shadows: tuple[int, ...]

    def validate(self, profile_count: int) -> None:
        _validate_weights("highlights", self.highlights, profile_count)
        _validate_weights("midtones", self.midtones, profile_count)
        _validate_weights("shadows", self.shadows, profile_count)


@dataclass(frozen=True, slots=True)
class QtrBlendConfiguration:
    """Whole-profile blending policy."""

    split_tone: bool
    weights: QtrToneBlendWeights


@dataclass(frozen=True, slots=True)
class QtrAdvancedAdjustments:
    """Global EpsonRIP runtime adjustments; zero is neutral."""

    gamma: int = 0
    ink_limit: int = 0

    def __post_init__(self) -> None:
        controls = (
            ("gamma", self.gamma, QTR_GAMMA_MIN, QTR_GAMMA_MAX),
            ("ink_limit", self.ink_limit, QTR_INK_LIMIT_MIN, QTR_INK_LIMIT_MAX),
        )
        for name, value, minimum, maximum in controls:
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} должен быть целым")
            if value < minimum or value > maximum:
                raise ValueError(
                    f"{name} должен быть в диапазоне "
                    f"{minimum}..{maximum}"
                )


@dataclass(frozen=True, slots=True)
class QtrRuntimeProfileSet:
    """One to three compatible complete compiled profiles."""

    profiles: tuple[QuadProfile, ...]
    blend: QtrBlendConfiguration
    adjustments: QtrAdvancedAdjustments = QtrAdvancedAdjustments()

    def __post_init__(self) -> None:
        if not 1 <= len(self.profiles) <= 3:
            raise ValueError("QTR runtime требует от 1 до 3 active profiles")
        self.blend.weights.validate(len(self.profiles))
        reference = set(self.profiles[0].ink_codes)
        for index, profile in enumerate(self.profiles[1:], start=2):
            current = set(profile.ink_codes)
            if current != reference:
                missing = sorted(reference - current)
                extra = sorted(current - reference)
                raise ValueError(
                    f"Curve {index} имеет несовместимое logical ink space; "
                    f"missing={missing}, extra={extra}"
                )

    @classmethod
    def single(cls, profile: QuadProfile) -> QtrRuntimeProfileSet:
        weights = QtrToneBlendWeights((100,), (100,), (100,))
        return cls((profile,), QtrBlendConfiguration(False, weights))

    @property
    def logical_inks(self) -> tuple[str, ...]:
        return self.profiles[0].ink_codes

    @property
    def is_neutral_single_profile(self) -> bool:
        return (
            len(self.profiles) == 1
            and self.blend.weights == QtrToneBlendWeights((100,), (100,), (100,))
            and self.adjustments == QtrAdvancedAdjustments()
        )


@dataclass(frozen=True, slots=True)
class QtrRuntimeCodeTrace:
    input_code: int
    gamma_adjusted_code: int
    profile_vectors: tuple[tuple[int, ...], ...]
    weight_numerators: tuple[int, ...]
    weight_denominator: int
    blended_vector: tuple[int, ...]
    final_vector: tuple[int, ...]

    @property
    def original_qtr_code(self) -> int:
        return self.input_code


def _validate_gamma(gamma: int) -> None:
    if isinstance(gamma, bool) or not isinstance(gamma, int):
        raise TypeError("gamma должен быть целым")
    if not QTR_GAMMA_MIN <= gamma <= QTR_GAMMA_MAX:
        raise ValueError(f"gamma должен быть в диапазоне {QTR_GAMMA_MIN}..{QTR_GAMMA_MAX}")


@cache
def _gamma_lut(gamma: int) -> tuple[int, ...]:
    _validate_gamma(gamma)
    if gamma == 0:
        return tuple(range(256))
    midpoint = 0.5 + gamma / 100.0
    exponent = log(midpoint) / log(0.5)
    values = []
    for code in range(256):
        if code in {0, 255}:
            values.append(code)
            continue
        tone = pow(code / 255.0, exponent)
        values.append(min(255, max(0, floor(255.0 * tone + 0.5))))
    return tuple(values)


def gamma_adjusted_code(code: int, gamma: int) -> int:
    """Map QTR tone before profile lookup.

    Gamma uses the approved midpoint-calibrated Levels-like transform in
    ``-15..15``; positive values darken midtones and negative values lighten.
    Quantization is half-up and codes 0/255 remain fixed.
    """

    if not 0 <= code <= 255:
        raise ValueError("QTR code должен быть в диапазоне 0..255")
    _validate_gamma(gamma)
    return _gamma_lut(gamma)[code]


def _weight_fraction(
    code: int, configuration: QtrBlendConfiguration
) -> tuple[tuple[int, ...], int]:
    weights = configuration.weights
    if not configuration.split_tone:
        return weights.highlights, 1
    if code <= QTR_MIDTONE_CODE:
        return (
            tuple(
                highlight * (QTR_MIDTONE_CODE - code) + midtone * code
                for highlight, midtone in zip(weights.highlights, weights.midtones, strict=True)
            ),
            QTR_MIDTONE_CODE,
        )
    span = QTR_SHADOW_CODE - QTR_MIDTONE_CODE
    return (
        tuple(
            midtone * (QTR_SHADOW_CODE - code)
            + shadow * (code - QTR_MIDTONE_CODE)
            for midtone, shadow in zip(weights.midtones, weights.shadows, strict=True)
        ),
        span,
    )


def _round_half_up(numerator: int, denominator: int) -> int:
    return (numerator + denominator // 2) // denominator


def _blend_values(
    values: tuple[int, ...], weight_numerators: tuple[int, ...], weight_denominator: int
) -> int:
    denominator = 100 * weight_denominator
    return _round_half_up(
        sum(
            value * weight
            for value, weight in zip(values, weight_numerators, strict=True)
        ),
        denominator,
    )


def _apply_ink_limit(value: int, adjustment: int) -> int:
    if adjustment == 0:
        return value
    scaled = _round_half_up(value * (100 + adjustment), 100)
    return min(QTR_UINT16_MAX, scaled)


def trace_runtime_code(code: int, runtime: QtrRuntimeProfileSet) -> QtrRuntimeCodeTrace:
    adjusted = gamma_adjusted_code(code, runtime.adjustments.gamma)
    profile_vectors = tuple(
        tuple(profile.curve(ink).samples[adjusted] for ink in runtime.logical_inks)
        for profile in runtime.profiles
    )
    weight_numerators, weight_denominator = _weight_fraction(code, runtime.blend)
    blended = tuple(
        _blend_values(
            tuple(vector[ink_index] for vector in profile_vectors),
            weight_numerators,
            weight_denominator,
        )
        for ink_index in range(len(runtime.logical_inks))
    )
    final = tuple(
        _apply_ink_limit(value, runtime.adjustments.ink_limit) for value in blended
    )
    return QtrRuntimeCodeTrace(
        input_code=code,
        gamma_adjusted_code=adjusted,
        profile_vectors=profile_vectors,
        weight_numerators=weight_numerators,
        weight_denominator=weight_denominator,
        blended_vector=blended,
        final_vector=final,
    )


def compile_runtime_lut(runtime: QtrRuntimeProfileSet, ink_code: str) -> tuple[int, ...]:
    if ink_code not in runtime.logical_inks:
        raise KeyError(ink_code)
    return tuple(
        trace_runtime_code(code, runtime).final_vector[runtime.logical_inks.index(ink_code)]
        for code in range(256)
    )


def compile_runtime_luts(runtime: QtrRuntimeProfileSet) -> dict[str, tuple[int, ...]]:
    """Compile every logical output in one 256-code pass."""

    columns: list[list[int]] = [[] for _ in runtime.logical_inks]
    for code in range(256):
        final = trace_runtime_code(code, runtime).final_vector
        for column, value in zip(columns, final, strict=True):
            column.append(value)
    return {
        ink: tuple(column)
        for ink, column in zip(runtime.logical_inks, columns, strict=True)
    }


def compile_raw_calibration_lut(
    adjustments: QtrAdvancedAdjustments,
) -> tuple[int, ...]:
    """Apply normal gamma and ink limit to raw codes without a QUAD curve."""

    return tuple(
        _apply_ink_limit(
            gamma_adjusted_code(code, adjustments.gamma) * 257,
            adjustments.ink_limit,
        )
        for code in range(256)
    )


def execute_runtime_curve_codes(
    code_rows: tuple[bytes, ...],
    runtime: QtrRuntimeProfileSet,
    ink_code: str,
    *,
    compiled_lut: tuple[int, ...] | None = None,
) -> LogicalContonePlane:
    """Execute one runtime-blended logical ink through a precompiled 256-node LUT."""

    if runtime.is_neutral_single_profile:
        return execute_curve_codes(code_rows, runtime.profiles[0].curve(ink_code))
    if not code_rows or not code_rows[0]:
        raise ValueError("QTR code raster dimensions должны быть положительными")
    width = len(code_rows[0])
    if any(len(row) != width for row in code_rows):
        raise ValueError("Все строки QTR code raster должны иметь одинаковую ширину")
    lut = np.asarray(
        compiled_lut if compiled_lut is not None else compile_runtime_lut(runtime, ink_code),
        dtype=np.uint16,
    )
    rows: list[array[int]] = []
    for row in code_rows:
        values = lut[np.frombuffer(row, dtype=np.uint8)]
        packed = array("H")
        packed.frombytes(values.tobytes())
        rows.append(packed)
    return LogicalContonePlane(LogicalChannelId(ink_code), width, tuple(rows))
