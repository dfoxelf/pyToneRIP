# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Media selectors verified in ReferenceDriver streams for specific Stylus Pro models.

The five listed printers send REMOTE1 MI with the first Paper.Param byte and
media-size byte zero. Other Stylus Pro generations may omit MI entirely, so
these records must not be inherited through a Gutenprint model family.
"""

from __future__ import annotations

from typing import NamedTuple


class CapturedMedia(NamedTuple):
    display_name: str
    gutenprint_name: str
    media_code: int


# ReferenceDriver Epson_StylusPro4900/7900/9900.tpp, checked against tpprint streams.
_PRO4900_MEDIA = (
    CapturedMedia("Plain Paper", "Plain", 0),
    CapturedMedia("Epson Inkjet Paper", "Inkjet", 29),
    CapturedMedia("Epson Matte Heavy", "ArchivalMatte", 15),
    CapturedMedia("Fine Art Paper", "WaterColorRadiant", 25),
    CapturedMedia("Epson Premium Glossy", "GlossyPhoto", 11),
    CapturedMedia("Baryta Paper", "Luster", 139),
)

# ReferenceDriver Epson_StylusPro7890/9890.tpp, checked against tpprint streams.
_PRO7890_MEDIA = (
    CapturedMedia("Plain Paper", "Plain", 11),
    CapturedMedia("Epson Inkjet Paper", "Inkjet", 1),
    CapturedMedia("Archival Matte", "ArchivalMatte", 2),
    CapturedMedia("Epson Photo Glossy", "GlossyPhoto", 4),
    CapturedMedia("Epson Proof Satin", "Semigloss", 23),
)

CAPTURED_PRO_MEDIA_BY_DRIVER = {
    "escp2-4900": _PRO4900_MEDIA,
    "escp2-7900": _PRO4900_MEDIA,
    "escp2-9900": _PRO4900_MEDIA,
    "escp2-7890": _PRO7890_MEDIA,
    "escp2-9890": _PRO7890_MEDIA,
}
