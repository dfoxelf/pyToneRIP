# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""QuadToneRIP profile parsing and logical continuous-tone execution."""

from xp750_rip.qtr.executor import (
    QtrExecutionResult,
    UnsupportedQtrSemanticsError,
    evaluate_profile,
    evaluate_profile_code,
)
from xp750_rip.qtr.qidf import QidfDocument, parse_qidf
from xp750_rip.qtr.quad import QuadProfile, parse_quad

__all__ = [
    "QidfDocument",
    "QtrExecutionResult",
    "QuadProfile",
    "UnsupportedQtrSemanticsError",
    "evaluate_profile",
    "evaluate_profile_code",
    "parse_qidf",
    "parse_quad",
]
"""QuadToneRIP input formats and execution."""

from xp750_rip.qtr.bundle import QtrProfileBundle, load_profile_bundle

__all__ = ["QtrProfileBundle", "load_profile_bundle"]
