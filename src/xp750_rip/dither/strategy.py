# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Generic halftone strategy selection above printer backends."""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from xp750_rip.contone import LogicalContonePlane
from xp750_rip.halftone import LogicalDotPlane


class HalftoneAlgorithm(StrEnum):
    ORDERED = "Ordered"
    ADAPTIVE_HYBRID = "Adaptive Hybrid"
    FAST = "Fast"
    VERY_FAST = "Very Fast"
    BLUE_NOISE_FM = "Blue Noise (FM)"
    OSTROMOUKHOV = "Ostromoukhov (2001)"
    ZHOU_FANG = "Zhou-Fang (2003)"
    ENTROPY_CONSTRAINED_ZHOU_FANG = "Entropy-Constrained Zhou-Fang"
    X2Y1 = "X2Y1"
    COUPLED_ED = "Coupled ED"
    COUPLED_ED_CM = "Coupled ED (C/M)"

    @property
    def is_coupled(self) -> bool:
        return self in (HalftoneAlgorithm.COUPLED_ED, HalftoneAlgorithm.COUPLED_ED_CM)

    @property
    def display_name(self) -> str:
        # Keep the existing full-pair ID readable by saved configurations.
        return "Coupled ED (C/M + LC/LM)" if self is HalftoneAlgorithm.COUPLED_ED else self.value

    @property
    def gutenprint_identifier(self) -> str:
        return {
            HalftoneAlgorithm.ORDERED: "Ordered",
            HalftoneAlgorithm.ADAPTIVE_HYBRID: "Adaptive",
            HalftoneAlgorithm.FAST: "Fast",
            HalftoneAlgorithm.VERY_FAST: "VeryFast",
            # This identifier deliberately records pyToneRIP provenance in
            # manifests; it is not part of Gutenprint's string-list API.
            HalftoneAlgorithm.BLUE_NOISE_FM: "pyToneRIP.BlueNoiseFM",
            HalftoneAlgorithm.OSTROMOUKHOV: "pyToneRIP.Ostromoukhov2001",
            HalftoneAlgorithm.ZHOU_FANG: "pyToneRIP.ZhouFang2003",
            HalftoneAlgorithm.X2Y1: "pyToneRIP.X2Y1",
            HalftoneAlgorithm.COUPLED_ED: "pyToneRIP.CoupledED",
            HalftoneAlgorithm.COUPLED_ED_CM: "pyToneRIP.CoupledED.CM",
            HalftoneAlgorithm.ENTROPY_CONSTRAINED_ZHOU_FANG: (
                "pyToneRIP.EntropyConstrainedZhouFang"
            ),
        }[self]


class HalftoneStrategy(Protocol):
    def dither_placed(
        self,
        plane: LogicalContonePlane,
        *,
        canvas_width: int,
        canvas_height: int,
        x: int = 0,
        y: int = 0,
    ) -> LogicalDotPlane: ...


def require_implemented_algorithm(value: str) -> HalftoneAlgorithm:
    try:
        algorithm = HalftoneAlgorithm(value)
    except ValueError as error:
        raise ValueError(f"Неизвестный halftone algorithm: {value}") from error
    return algorithm
