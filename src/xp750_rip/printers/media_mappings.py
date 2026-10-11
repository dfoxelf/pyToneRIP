# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Shared PPD MediaType to native Epson ESC/P2 mappings and UI normalization."""

from __future__ import annotations

import re
from collections.abc import Callable, Hashable, Iterable

from xp750_rip.capabilities.ppd import PpdChoice

_QUALITY_SUFFIXES = ("_NORMAL", "_HIGH", "_DRAFT")
_QUALITY_PRIORITY = {"_NORMAL": 0, "_HIGH": 1, "_DRAFT": 2}
_DISPLAY_QUALITY_SUFFIX = re.compile(
    r"\s*[-\N{EN DASH}\N{EM DASH}]\s*(?:draft|standard|best|high)\s*$",
    re.IGNORECASE,
)

# PPD ID -> (Gutenprint artisan medium, REMOTE1 PaperMedia, PrintMethod family).
# The PPD quality suffix is ESC/P-R metadata; the selected native mode controls
# the actual resolution/quality in our ESC/P2 backend.
MEDIA_MAPPINGS: dict[str, tuple[str, int, str]] = {
    "PLATINA_NORMAL": ("UltraGlossyPhoto", 38, "photo"),
    "PLATINA_HIGH": ("UltraGlossyPhoto", 38, "photo"),
    "PMPHOTO_NORMAL": ("GlossyPhoto", 11, "photo"),
    "PMPHOTO_HIGH": ("GlossyPhoto", 11, "photo"),
    "PMPHOTO_DRAFT": ("GlossyPhoto", 11, "photo"),
    # XP-950's exact Epson PPD calls Premium Glossy GPPAPER; ReferenceDriver and
    # Gutenprint identify that material with the same native MI 11.
    "GPPAPER_NORMAL": ("GlossyPhoto", 11, "photo"),
    "GPPAPER_HIGH": ("GlossyPhoto", 11, "photo"),
    "GLOSSYPHOTO_NORMAL": ("Photo", 6, "photo"),
    "GLOSSYPHOTO_HIGH": ("Photo", 6, "photo"),
    "GLOSSYPHOTO_DRAFT": ("Photo", 6, "photo"),
    "PSGLOS_NORMAL": ("Semigloss", 12, "photo"),
    "PSGLOS_HIGH": ("Semigloss", 12, "photo"),
    "PSGLOS_DRAFT": ("Semigloss", 12, "photo"),
    "GLOSSYCAST_NORMAL": ("Photo", 6, "photo"),
    "GLOSSYCAST_HIGH": ("Photo", 6, "photo"),
    "GLOSSYCAST_DRAFT": ("Photo", 6, "photo"),
    # XP-970/980 call the basic photo paper LCPP in Epson's PPD. The shared
    # model-96 artisan media table names that paper Photo (MI 6).
    "LCPP_NORMAL": ("Photo", 6, "photo"),
    "LCPP_HIGH": ("Photo", 6, "photo"),
    "PMMATT_NORMAL": ("MatteHeavy", 5, "matte"),
    "PMMATT_HIGH": ("MatteHeavy", 5, "matte"),
    "SFINE_HIGH": ("Coated", 3, "coated"),
    "PLAIN_NORMAL": ("Plain", 0, "plain"),
    "PLAIN_HIGH": ("Plain", 0, "plain"),
    "PLAIN_DRAFT": ("Plain", 0, "plain"),
    "LETTERHEAD_NORMAL": ("Plain", 0, "plain"),
    "LETTERHEAD_HIGH": ("Plain", 0, "plain"),
    "LETTERHEAD_DRAFT": ("Plain", 0, "plain"),
    "IRON_NORMAL": ("Inkjet", 1, "plain"),
    "RCPC_NORMAL": ("Postcard", 5, "plain"),
    "RCPC_HIGH": ("Postcard", 5, "plain"),
    "ENV_NORMAL": ("Envelope", 9, "envelope"),
    "ENV_HIGH": ("Envelope", 9, "envelope"),
    "MINIPHOTO_NORMAL": ("GlossyPhoto", 11, "photo"),
    "MINIPHOTO_HIGH": ("GlossyPhoto", 11, "photo"),
    "CDDVD_HIGH": ("CD", 91, "cd"),
    "CDDVDHIGH_HIGH": ("CD", 91, "cd"),
}


def media_material_id(identity: str) -> str:
    """Return the material part of an Epson PPD media ID, without quality."""

    for suffix in _QUALITY_SUFFIXES:
        if identity.endswith(suffix):
            return identity[: -len(suffix)]
    return identity


def media_display_name(label: str) -> str:
    """Remove PPD quality wording because native quality is selected by mode."""

    return _DISPLAY_QUALITY_SUFFIX.sub("", label).strip()


def canonical_media_choices(
    choices: Iterable[PpdChoice],
    *,
    equivalence_key: Callable[[PpdChoice], Hashable] | None = None,
) -> tuple[tuple[PpdChoice, ...], tuple[tuple[str, str], ...]]:
    """Collapse Draft/Standard/Best variants of the same physical material.

    Choice objects only need ``name`` and ``label`` attributes.  NORMAL is the
    preferred stable ID, then HIGH, then DRAFT.  Discarded IDs are returned as
    aliases so configurations produced by earlier releases remain readable.
    By default, different named materials are not merged merely because an
    Epson model happens to encode them with the same PaperMedia value. Callers
    that also know the PPD constraints may provide a stronger equivalence key.
    """

    grouped: dict[Hashable, list[PpdChoice]] = {}
    order: list[Hashable] = []
    for choice in choices:
        material = (
            equivalence_key(choice)
            if equivalence_key is not None
            else media_material_id(choice.name)
        )
        if material not in grouped:
            grouped[material] = []
            order.append(material)
        grouped[material].append(choice)

    canonical: list[PpdChoice] = []
    aliases: list[tuple[str, str]] = []
    for material in order:
        variants = grouped[material]

        def quality_rank(choice: PpdChoice) -> int:
            name = choice.name
            return next(
                (_QUALITY_PRIORITY[suffix] for suffix in _QUALITY_SUFFIXES if name.endswith(suffix)),
                len(_QUALITY_PRIORITY),
            )

        selected = min(variants, key=quality_rank)
        canonical.append(selected)
        aliases.extend(
            (choice.name, selected.name)
            for choice in variants
            if choice.name != selected.name
        )
    return tuple(canonical), tuple(aliases)


def supported_media_modes(identity: str, mode_ids: tuple[str, ...]) -> tuple[str, ...]:
    """Apply conservative native-mode limits to mechanically special media."""

    material = media_material_id(identity)
    maximum_by_material = {
        # Flexible transfer and envelope stock are limited to 720 DPI.
        "IRON": "720sw",
        "ENV": "720sw",
        # Thick postcard stock stops before oversampled/high-density modes.
        "RCPC": "1440x720sw",
        # Photo stickers retain photographic quality without extreme modes.
        "MINIPHOTO": "1440x1440ov",
    }
    maximum = maximum_by_material.get(material)
    if maximum is None:
        return mode_ids
    try:
        return mode_ids[: mode_ids.index(maximum) + 1]
    except ValueError:
        # Imported models may expose a different mode set. Keep only the known
        # low-to-high prefix when the named boundary exists; otherwise do not
        # pretend that an arbitrary imported ordering is equivalent.
        return mode_ids


def media_print_methods(
    family: str, mode_ids: tuple[str, ...]
) -> tuple[tuple[str, int], ...]:
    """Return the model-96 PrintMethod for each available native mode."""

    result: list[tuple[str, int]] = []
    for mode in mode_ids:
        if family == "plain":
            value = 0x10 if mode == "360x180sw" else 0x21 if mode in {
                "360sw", "720x360sw"
            } else 0x51
        elif family == "photo":
            if mode in {"360x180sw", "360sw", "720x360sw"}:
                value = 0x41
            elif mode in {"720sw", "1440x720sw"}:
                value = 0x52
            elif mode in {"1440x1440ov", "2880x1440sw"}:
                value = 0x55
            else:
                value = 0xB2
        elif family == "matte":
            value = 0xB2 if mode in {
                "5760x1440sw", "2880x2880sw", "5760x2880sw"
            } else 0x55
        elif family == "coated":
            value = 0x55
        elif family == "envelope":
            value = 0x20 if mode in {"360x180sw", "360sw"} else (
                0x40 if mode == "720x360sw" else 0x50
            )
        elif family == "cd":
            value = 0xB1
        else:
            raise ValueError(f"Unknown media PrintMethod family: {family}")
        result.append((mode, value))
    return tuple(result)
