# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Generic capability readers independent of printer backends."""

from xp750_rip.capabilities.ppd import (
    PpdCapabilities,
    PpdChoice,
    PpdConstraint,
    PpdOption,
    PpdPage,
    parse_ppd,
    read_ppd,
)

__all__ = [
    "PpdCapabilities",
    "PpdChoice",
    "PpdConstraint",
    "PpdOption",
    "PpdPage",
    "parse_ppd",
    "read_ppd",
]
