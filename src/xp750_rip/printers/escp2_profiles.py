# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Data-only Epson ESC/P2 family profiles imported from the research corpus."""

from __future__ import annotations

from xp750_rip.printers.capabilities import EpsonEscp2BackendProfile


def _profile(
    identity: str,
    units: str,
    printhead: str,
    rows: int,
    dot_size: int,
    print_method: int | None,
    *,
    remote_extras: tuple[tuple[str, bytes], ...] = (),
) -> EpsonEscp2BackendProfile:
    return EpsonEscp2BackendProfile(
        identity=identity,
        units_payload=bytes.fromhex(units),
        printhead_resolution_payload=bytes.fromhex(printhead),
        rows_per_pass=rows,
        default_dot_size=dot_size,
        default_print_method=print_method,
        remote_extras=remote_extras,
        source="research family profiles and captured Epson PRN streams",
    )


# These are protocol payloads, not marketing-model aliases. Multiple models can
# safely reuse a profile only when the source assigns the same driver family.
# Это протокольные payload, а не сходство названий принтеров: профиль выбирается
# строго по family из экспортированного .tpp.
ESCP2_FAMILY_PROFILES = {
    "PX700W": _profile("PX700W", "020201400b", "400b1004", 180, 0x25, 0xB2),
    "STP950": _profile("STP950", "020202a005", "a0050804", 96, 0x11, None),
    "L8050": _profile("L8050", "020201400b", "400b1004", 180, 0x13, 0xB0),
    "ET8550": _profile("ET8550", "020201400b", "400b1004", 180, 0x13, 0xB0),
    "R2400": _profile("R2400", "020201400b", "400b1004", 180, 0x12, 0x90),
    "R2880": _profile("R2880", "020201400b", "400b1004", 180, 0x12, 0xA0),
    "R3000": _profile("R3000", "020201400b", "400b1004", 180, 0x14, 0xA0),
    "PRO3880": _profile("PRO3880", "020201400b", "400b0201", 1, 0x12, None),
    "R1900": _profile("R1900", "020201400b", "400b1004", 180, 0x13, 0xB0),
    "XP15000": _profile("XP15000", "010101400b", "400b1004", 180, 0x14, 0xB0),
    "XP305": _profile("XP305", "020201400b", "400b1008", 60, 0x13, 0xA2),
    "WF7610": _profile("WF7610", "010101b004", "b0040404", 128, 0x12, 0xB0),
    "SC-T5400": _profile("SC-T5400", "0202016009", "60090804", 144, 0x11, 0xC0),
    # T3200 reference stream: ESC (U 02 02 01 40 0b, ESC (D 40 0b 02 01,
    # ESC (e 00 05, ESC (m 00. T3200/T5200/T7200 share driver Id T7200.
    # Epson specifies 720 nozzles per channel for this physical family.
    "T7200": _profile("T7200", "020201400b", "400b0201", 720, 0x05, 0x00),
    "P700RIP": _profile(
        "P700RIP",
        "020201400b",
        "400b1004",
        180,
        0x12,
        0x80,
    ),
}


def gutenprint_profile(
    family: str,
    *,
    rows_per_pass: int,
    source: str,
) -> EpsonEscp2BackendProfile:
    """Create a profile whose page-init units are derived from each GP mode."""

    return EpsonEscp2BackendProfile(
        identity=family,
        # Required structural placeholders are ignored when mode_derived_units
        # is true; keeping them valid makes serialization and validation simple.
        units_payload=b"\x01\x01\x01\x80\x16",
        printhead_resolution_payload=b"\x40\x0b\x10\x04",
        rows_per_pass=rows_per_pass,
        default_dot_size=0,
        default_print_method=None,
        source=source,
        mode_derived_units=True,
    )
