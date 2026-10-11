# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""ESC/P2 encoder package with cycle-safe legacy lazy exports."""

from __future__ import annotations

from typing import Any

__all__ = ["Escp2Channel", "GeneratedJob", "PrinterModel", "generate_phase2_job"]


def __getattr__(name: str) -> Any:
    if name in {"GeneratedJob", "generate_phase2_job"}:
        from xp750_rip.escp2 import job

        return getattr(job, name)
    if name in {"Escp2Channel", "PrinterModel"}:
        from xp750_rip.escp2 import model

        return getattr(model, name)
    raise AttributeError(name)
