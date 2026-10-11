# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Logical labels for the XP-750's six addressable physical ink paths."""

from enum import StrEnum


class PhysicalChannel(StrEnum):
    """Stable path labels; they describe routing, not installed ink chemistry."""

    K = "K"
    C = "C"
    M = "M"
    Y = "Y"
    LC = "LC"
    LM = "LM"
