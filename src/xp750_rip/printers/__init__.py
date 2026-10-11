# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Printer descriptors; public objects are imported lazily to avoid backend cycles."""

from __future__ import annotations

from typing import Any

__all__ = [
    "XP750",
    "PrinterInstance",
    "PrinterModel",
    "PrinterRegistry",
    "default_registry",
    "load_xp750_model",
    "load_xp760_model",
    "load_xp960_model",
]


def __getattr__(name: str) -> Any:
    if name == "XP750":
        from xp750_rip.printers.xp750 import XP750

        return XP750
    if name in {"PrinterInstance", "PrinterModel", "PrinterRegistry"}:
        from xp750_rip.printers import capabilities

        return getattr(capabilities, name)
    if name in {"default_registry", "load_xp750_model", "load_xp760_model", "load_xp960_model"}:
        from xp750_rip.printers import xp750_capabilities

        return getattr(xp750_capabilities, name)
    raise AttributeError(name)
