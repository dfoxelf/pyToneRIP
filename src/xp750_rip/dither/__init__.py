# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Source-derived halftone implementations."""

from xp750_rip.dither.blue_noise import BlueNoiseFMDither, load_blue_noise_fm_matrix
from xp750_rip.dither.error_diffusion import VariableCoefficientErrorDiffusion
from xp750_rip.dither.fast import GutenprintVeryFastDither
from xp750_rip.dither.ordered import (
    GutenprintDropModel,
    GutenprintOrderedDither,
    OrderedDitherMatrix,
    load_gutenprint_fast_matrix,
    load_gutenprint_matrix,
    load_gutenprint_matrix_2x1,
    unpack_sample_codes,
)
from xp750_rip.dither.strategy import (
    HalftoneAlgorithm,
    HalftoneStrategy,
    require_implemented_algorithm,
)

__all__ = [
    "GutenprintDropModel",
    "GutenprintOrderedDither",
    "GutenprintVeryFastDither",
    "BlueNoiseFMDither",
    "HalftoneAlgorithm",
    "HalftoneStrategy",
    "OrderedDitherMatrix",
    "VariableCoefficientErrorDiffusion",
    "load_blue_noise_fm_matrix",
    "load_gutenprint_fast_matrix",
    "load_gutenprint_matrix",
    "load_gutenprint_matrix_2x1",
    "require_implemented_algorithm",
    "unpack_sample_codes",
]
