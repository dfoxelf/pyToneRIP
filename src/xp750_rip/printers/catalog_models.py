# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Convert persistent catalog packages into runtime PrinterModel objects.

The converter is intentionally deterministic: all information required by the
installed RIP is copied into the catalog during import. No external driver or
research installation is consulted at runtime.
"""

from __future__ import annotations

import json
import re
from dataclasses import fields, replace
from pathlib import Path
from typing import Any

from xp750_rip.capabilities.ppd import PpdCapabilities, PpdPage, read_ppd
from xp750_rip.printers.capabilities import (
    DeviceSettingCapability,
    DeviceSettingChoice,
    InkConfigurationCapability,
    InputSourceCapability,
    MediaCapability,
    PageCapability,
    PhysicalPathDefinition,
    PrinterMode,
    PrinterModel,
)
from xp750_rip.printers.catalog import (
    CATALOG_ROOT,
    CatalogPackage,
    load_catalog_packages,
)
from xp750_rip.printers.escp2_profiles import ESCP2_FAMILY_PROFILES, gutenprint_profile
from xp750_rip.printers.media_mappings import (
    canonical_media_choices,
    media_display_name,
    media_material_id,
)
from xp750_rip.printers.verified_reference_driver_pro_media import (
    CAPTURED_PRO_MEDIA_BY_DRIVER,
)
from xp750_rip.raster import PhysicalPathId


_MODEL_SPECIFIC_FEED_PACKAGES = frozenset({
    # Restored packages: compatible Gutenprint backends contain generic slots.
    "pf--Epson_EcoTank_ET7700", "pf--Epson_EcoTank_ET7750",
    "pf--Epson_L11050", "pf--Epson_L7180", "pf--Epson_XP245",
    # ReferenceDriver XP-510 and XP-820 technical driver families.
    "pf--Epson_XP510series", "pf--Epson_XP520series",
    "pf--Epson_XP530series", "pf--Epson_XP540series",
    "pf--Epson_XP620series", "pf--Epson_XP630series",
    "pf--Epson_XP640series", "pf--Epson_XP7100series",
    "pf--Epson_XP720series", "pf--Epson_XP820series",
    "pf--Epson_XP830series",
})


def _integers(value: object) -> tuple[int, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(int(item) for item in value)
    return tuple(int(item) for item in re.findall(r"-?\d+", str(value or "")))


def _parameter_byte(
    parameters: dict[str, Any], name: str, *, context: str
) -> int | None:
    values = _integers(parameters.get(name))
    if not values:
        return None
    value = values[0]
    if not 0 <= value <= 0xFF:
        raise ValueError(f"{context} {name} must fit one protocol byte")
    return value


def _pair(value: object, default: tuple[int, int]) -> tuple[int, int]:
    values = _integers(value)
    if len(values) < 2 or values[0] <= 0 or values[1] <= 0:
        return default
    return values[0], values[1]


def _identifier(value: str, fallback: str) -> str:
    token = re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_")
    return token or fallback


def _media_class(value: str) -> str:
    text = value.casefold()
    if any(token in text for token in ("plain", "normal", "draft", "postcard", "letter")):
        return "plain"
    if any(token in text for token in ("matte", "fineart", "canvas", "velvet")):
        return "matte"
    if any(token in text for token in ("gloss", "photo", "luster", "lustre", "semi")):
        return "photo"
    return "generic"


_THIRD_PARTY_MEDIA_MARKERS = (
    "archival",
    "dtf",
    "dye sublimation",
    "hahnem",
    "ilford",
    "kodak",
    "proof paper",
    "silk screen fine drops",
    "fuji",
    "zedonet",
)


def _is_third_party_medium(medium: MediaCapability) -> bool:
    text = f"{medium.identity} {medium.display_name}".casefold()
    return any(marker in text for marker in _THIRD_PARTY_MEDIA_MARKERS)


def _media_preference(medium: MediaCapability) -> tuple[int, int, str]:
    """Prefer a standard material name when native behavior is identical.

    The RIP is a monochrome driver, not a paper-profile browser. Branded media
    aliases that emit the same native commands only make the UI longer and do
    not add printer functionality. / RIP показывает физически разные режимы
    носителя, а не маркетинговые синонимы одного и того же набора команд.
    """

    text = medium.display_name.casefold()
    if "exact Epson PPD MediaType" in medium.source:
        return -100, len(text), text
    if _is_third_party_medium(medium):
        return 1000, len(text), text
    if text in {"other", "special media", "generic"}:
        return 900, len(text), text
    if any(
        token in text
        for token in (
            "plain paper",
            "inkjet paper",
            "matte paper",
            "photo paper",
            "premium glossy",
            "premium semigloss",
            "premium luster",
            "premium lustre",
        )
    ):
        return 0, len(text), text
    if any(token in text for token in ("envelope", "postcard", "film", "transparen")):
        return 200, len(text), text
    return 100, len(text), text


def _canonical_media(
    media: tuple[MediaCapability, ...],
    modes: tuple[PrinterMode, ...],
    *,
    unique_media_codes: bool = False,
    prefer_technical_media: bool = False,
    prefer_product_media: bool = False,
    preferred_product_media_by_code: dict[int, str] | None = None,
) -> tuple[tuple[MediaCapability, ...], tuple[tuple[str, str], ...]]:
    """Expose at most one paper choice for each native MI value."""

    mode_by_id = {mode.identity: mode for mode in modes}
    grouped: dict[tuple[object, ...], list[MediaCapability]] = {}
    order: list[tuple[object, ...]] = []
    for medium in media:
        print_methods = dict(medium.print_methods)
        mode_mechanics = tuple(
            (
                mode.horizontal_dpi,
                mode.vertical_dpi,
                mode.physical_horizontal_dpi,
                mode.physical_vertical_dpi,
                mode.base_resolution_dpi,
                mode.horizontal_passes,
                mode.vertical_passes,
                mode.nozzle_count,
                mode.nozzle_separation,
                mode.bit_depth,
                mode.dot_size,
                mode.drop_sizes,
                mode.density,
                mode.ordered_matrix_id,
                mode.escp2_dot_size_override,
                print_methods.get(mode_id, mode.escp2_print_method_override),
                mode.escp2_printer_weave_mode,
                mode.escp2_alternate_print_method,
                mode.escp2_default_direction,
                mode.escp2_emit_legacy_s_mode_2,
                mode.escp2_internal_layout_flags,
                mode.escp2_weave_pass_group_count,
                mode.escp2_weave_variant_code,
                mode.escp2_explicit_weave_sequence,
            )
            for mode_id in medium.supported_modes
            if (mode := mode_by_id.get(mode_id)) is not None
        )
        if unique_media_codes and medium.media_code is not None:
            # The public media choice is the protocol MI value. Paper brand,
            # quality suffix and automatic feed/thickness defaults do not
            # create another paper type for the monochrome RIP.
            key: tuple[object, ...] = ("MI", medium.media_code)
        else:
            key = (
                "NATIVE",
                medium.media_code,
                medium.legacy_media_type_code,
                medium.special_media_selector,
                medium.physical_raster_layout_variant,
                medium.paper_thickness_code,
                medium.vacuum_intensity_code,
                medium.feed_adjustment_code,
                medium.feed_sequence_code,
                medium.platen_gap_code,
                mode_mechanics,
            )
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(medium)

    canonical: list[MediaCapability] = []
    aliases: list[tuple[str, str]] = []
    for key in order:
        candidates = grouped[key]
        if prefer_product_media and key[0] == "MI":
            product = [
                item for item in candidates
                if "normalized Epson product media mechanics" in item.source
            ]
            candidates_for_selection = product or candidates
        elif prefer_technical_media and key[0] == "MI":
            technical = [
                item for item in candidates
                if "normalized Epson product media mechanics" not in item.source
            ]
            candidates_for_selection = technical or candidates
        else:
            candidates_for_selection = candidates
        standard = [
            item for item in candidates_for_selection
            if not _is_third_party_medium(item)
        ]
        selected = min(standard or candidates_for_selection, key=_media_preference)
        if key[0] == "MI" and preferred_product_media_by_code:
            preferred_name = preferred_product_media_by_code.get(int(key[1]))
            selected = next(
                (
                    item for item in candidates_for_selection
                    if item.display_name == preferred_name
                ),
                selected,
            )
        used_ids = (
            {item.identity.casefold() for item in canonical}
            if prefer_product_media else set()
        )
        if selected.identity.casefold() in used_ids:
            selected = next(
                (
                    item for item in sorted(candidates_for_selection, key=_media_preference)
                    if item.identity.casefold() not in used_ids
                    and item.media_class == selected.media_class
                ),
                selected,
            )
        if selected.identity.casefold() in used_ids:
            # Different MI values can carry the same paper name (P800 has
            # two Plain Paper selectors). Keep both codes without publishing
            # duplicate case-insensitive IDs in the Manager's media list.
            code_suffix = f"MI{selected.media_code:02X}" if selected.media_code is not None else "NoMI"
            selected = replace(
                selected,
                identity=f"{selected.identity}_{code_suffix}",
                display_name=f"{selected.display_name} ({code_suffix})",
            )
        if unique_media_codes and key[0] == "MI":
            # Feed, thickness, vacuum and platen gap are user settings. A
            # branded ReferenceDriver paper must not hide a resolution merely because
            # it carries different defaults for those settings.
            native_fields = (
                "legacy_media_type_code", "special_media_selector",
                "physical_raster_layout_variant",
            )
            native_signature = tuple(getattr(selected, name) for name in native_fields)
            supported = list(selected.supported_modes)
            selected_methods = dict(selected.print_methods)
            signatures = {
                (*tuple(
                    getattr(mode_by_id[mode_id], field.name)
                    for field in fields(PrinterMode)
                    if field.name not in {"identity", "display_name", "source", "media_class"}
                ), selected_methods.get(mode_id))
                for mode_id in supported
                if mode_id in mode_by_id
            }
            for item in candidates:
                if tuple(getattr(item, name) for name in native_fields) != native_signature:
                    continue
                item_methods = dict(item.print_methods)
                for mode_id in item.supported_modes:
                    mode = mode_by_id.get(mode_id)
                    if mode is None or mode_id in supported:
                        continue
                    signature = (*tuple(
                        getattr(mode, field.name)
                        for field in fields(PrinterMode)
                        if field.name not in {"identity", "display_name", "source", "media_class"}
                    ), item_methods.get(mode_id))
                    if signature not in signatures:
                        supported.append(mode_id)
                        signatures.add(signature)
                        if mode_id in item_methods:
                            selected_methods[mode_id] = item_methods[mode_id]
            selected = replace(
                selected,
                supported_modes=tuple(supported),
                print_methods=tuple(selected_methods.items()),
            )
        canonical.append(selected)
        aliases.extend(
            (item.identity, selected.identity)
            for item in candidates
            if item.identity != selected.identity
            and item.identity.casefold() not in used_ids
            and (not prefer_product_media or item.media_class == selected.media_class)
        )

    if not canonical:
        raise ValueError("Imported printer has no standard media capabilities")
    return tuple(canonical), tuple(aliases)


_PPD_SOURCE_MEDIA_TOKENS: dict[str, tuple[str, ...]] = {
    "PLAIN": ("plain paper",),
    "PMMATT": ("matte paper heavyweight", "matte heavy", "matte paper"),
    "PMPHOTO": (
        "premium glossy photo",
        "premium glossy",
        "photo paper",
        "photo glossy",
    ),
    "GPPAPER": ("premium glossy photo", "premium glossy"),
    "PLATINA": ("ultra glossy",),
    "PSGLOS": ("premium semigloss", "premium satin"),
    "GLOSSYPHOTO": ("photo paper", "photo glossy"),
    "GLOSSYCAST": ("photo paper", "photo glossy"),
    "LCPP": ("photo paper", "photo glossy"),
    "SFINE": ("photo quality inkjet", "inkjet paper"),
    "VELVET_FINE": ("velvet fine",),
    "ENV": ("envelope",),
    "CDDVD": ("cd",),
    "CDDVDHIGH": ("cd verbatim", "cd premium"),
    "MINIPHOTO": ("photo sticker",),
    "THICK": ("thick paper", "card stock"),
    "THICK1": ("thick paper1",),
    "THICK2": ("thick paper2",),
}


def _ppd_named_media(
    media: tuple[MediaCapability, ...],
    ppd: PpdCapabilities,
    *,
    include_unmatched_native: bool,
) -> tuple[tuple[MediaCapability, ...], tuple[tuple[str, str], ...]]:
    """Name EcoTank mechanics from its exact Epson PPD.

    We retain the imported native feed/mechanical values while replacing
    profile-oriented aliases with the product PPD's material names. Unique
    native media absent from the RGB-driver PPD may optionally remain (for
    example a CD or film selector with genuinely different commands).
    """

    try:
        choices = ppd.option("MediaType").choices
    except KeyError:
        return media, ()
    canonical, quality_aliases = canonical_media_choices(choices)
    normalized: list[MediaCapability] = []
    mapped_ids: set[str] = set()
    used_source_ids: set[str] = set()
    for choice in canonical:
        material = media_material_id(choice.name)
        tokens = _PPD_SOURCE_MEDIA_TOKENS.get(material, ())
        source_medium = next(
            (
                item
                for token in tokens
                for item in media
                if token in item.display_name.casefold()
            ),
            None,
        )
        if source_medium is None:
            continue
        mapped_ids.add(choice.name)
        used_source_ids.add(source_medium.identity)
        normalized.append(
            replace(
                source_medium,
                identity=choice.name,
                display_name=media_display_name(choice.label),
                source=f"{source_medium.source} + exact Epson PPD MediaType",
            )
        )
    if include_unmatched_native:
        normalized.extend(
            item for item in media if item.identity not in used_source_ids
        )
    if not normalized:
        return media, ()
    return tuple(normalized), tuple(
        (alias, target)
        for alias, target in quality_aliases
        if target in mapped_ids
    )


def _flatten_aliases(
    aliases: tuple[tuple[str, str], ...],
) -> tuple[tuple[str, str], ...]:
    mapping = dict(aliases)
    result: list[tuple[str, str]] = []
    for alias, initial_target in mapping.items():
        target = initial_target
        visited = {alias}
        while target in mapping and target not in visited:
            visited.add(target)
            target = mapping[target]
        result.append((alias, target))
    return tuple(result)


def _p700_channels() -> list[dict[str, object]]:
    # The P700/P900 physical platform has ten installed inks. Photo Black and
    # Matte Black are alternative native addresses selected by media, so both
    # addressable paths are exposed with media guards.
    return [
        {"slot": "pb", "role": "photo_black", "esc_i_code": "0x00", "media": ["photo"]},
        {"slot": "mk", "role": "matte_black", "esc_i_code": "0x05", "media": ["plain", "matte"]},
        {"slot": "v", "role": "violet", "esc_i_code": "0x08"},
        {"slot": "m", "role": "vivid_magenta", "esc_i_code": "0x01"},
        {"slot": "c", "role": "cyan", "esc_i_code": "0x02"},
        {"slot": "y", "role": "yellow", "esc_i_code": "0x04"},
        {"slot": "gy", "role": "gray", "esc_i_code": "0x10"},
        {"slot": "lm", "role": "vivid_light_magenta", "esc_i_code": "0x11"},
        {"slot": "lc", "role": "light_cyan", "esc_i_code": "0x12"},
        {"slot": "lgy", "role": "light_gray", "esc_i_code": "0x30"},
    ]


def _choose_gutenprint_channels(channels: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in channels:
        grouped.setdefault(str(item.get("inkset") or ""), []).append(item)
    if not grouped:
        return []
    physical = [name for name in grouped if name.casefold().startswith("physical")]
    candidates = physical or list(grouped)
    selected_name = max(candidates, key=lambda name: (len(grouped[name]), name))
    result: list[dict[str, Any]] = []
    selectors: set[int] = set()
    for item in grouped[selected_name]:
        selector = int(str(item.get("esc_i_code", "0")), 0)
        if selector in selectors:
            continue
        selectors.add(selector)
        result.append(item)
    return result


_DIRECT_PHYSICAL_CHANNELS: dict[str, tuple[tuple[str, str, str], ...]] = {
    "XP510": (
        ("matte_k", "pigment_matte_black", "0x00"),
        ("photo_k", "photo_black", "0x40"),
        ("m", "magenta", "0x01"),
        ("c", "cyan", "0x02"),
        ("y", "yellow", "0x04"),
    ),
    "XP800": (
        ("matte_k", "pigment_matte_black", "0x00"),
        ("photo_k", "photo_black", "0x40"),
        ("m", "magenta", "0x01"),
        ("c", "cyan", "0x02"),
        ("y", "yellow", "0x04"),
    ),
    "ET14100": (
        ("k", "black", "0x00"),
        ("m", "magenta", "0x01"),
        ("c", "cyan", "0x02"),
        ("y", "yellow", "0x04"),
    ),
    "T3100": (
        ("k", "black", "0x00"),
        ("m", "magenta", "0x01"),
        ("c", "cyan", "0x02"),
        ("y", "yellow", "0x04"),
    ),
    "ET8550": (
        # Captured ET-8500/8550 streams and the ET8550 branch of the
        # ReferenceDriver raster writer confirm these physical addresses.
        # Its internal 0x100 gray address is packed into ESC i byte 0x10.
        ("k", "pigment_matte_black", "0x40"),
        ("photo_k", "photo_black", "0x00"),
        ("gray", "gray", "0x10"),
        ("m", "magenta", "0x01"),
        ("c", "cyan", "0x02"),
        ("y", "yellow", "0x04"),
    ),
}


# ReferenceDriver printers.lst maps these models to technical descriptors with
# Driver Id=XP305 and Cartridge0 Param=60,180,180,180. ReferenceDriver reference jobs
# for XP-2100 and ET-2810 stagger M/Y behind coincident K/C. Gutenprint's
# matching model 80/135 inksets give the physical base offsets 120/240 at
# 360 dpi. Gutenprint's XP-245 model-121 mapping conflicts with Epson's
# 180-black/59-color nozzle specification shared with XP-240/XP-2100;
# it must not override the exact ReferenceDriver XP-240 -> XP-320 mapping.
_MATCHED_FOUR_COLOR_HEAD_OFFSETS = {
    "pf--Epson_EcoTank_ET1810",
    "pf--Epson_EcoTank_ET2500",
    "pf--Epson_EcoTank_ET2550",
    "pf--Epson_EcoTank_ET2600",
    "pf--Epson_EcoTank_ET2650",
    "pf--Epson_EcoTank_ET2700",
    "pf--Epson_EcoTank_ET2710",
    "pf--Epson_EcoTank_ET2720",
    "pf--Epson_EcoTank_ET2750",
    "pf--Epson_EcoTank_ET2810",
    "pf--Epson_EcoTank_ET2820",
    "pf--Epson_EcoTank_ET2850",
    "pf--Epson_EcoTank_ET2860",
    "pf--Epson_EcoTank_ET2870",
    "pf--Epson_EcoTank_ET4500",
    "pf--Epson_EcoTank_ET4700",
    "pf--Epson_EcoTank_ET4800",
    "pf--Epson_L3110",
    "pf--Epson_XP2100series",
    "pf--Epson_XP2200series",
    "pf--Epson_XP240series",
    "pf--Epson_XP245",
    "pf--Epson_XP300series",
    "pf--Epson_XP3100series",
    "pf--Epson_XP310series",
    "pf--Epson_XP3200series",
    "pf--Epson_XP320series",
    "pf--Epson_XP330series",
    "pf--Epson_XP340series",
    "pf--Epson_XP400series",
    "pf--Epson_XP4100series",
    "pf--Epson_XP410series",
    "pf--Epson_XP4200series",
    "pf--Epson_XP420series",
    "pf--Epson_XP430series",
    "pf--Epson_XP440series",
}

# A live ET-2550 print placed the cropped, non-borderless A6 raster at the
# sheet edge. Its Epson PPD specifies 8.4 pt (about 3 mm) on every side.
# Other consumer models keep their existing native origin until checked.
_PAGE_EDGE_RASTER_ORIGIN_PACKAGES = {
    "pf--Epson_EcoTank_ET2550",
    "pf--Epson_SureColorP400",
    "pf--Epson_SureColorP600",
    "pf--Epson_SureColorP700_RIP",
    "pf--Epson_SureColorP800",
    "pf--Epson_SureColorP900_RIP",
    "pf--Epson_SureColorF500",
    "pf--Epson_SureColorT3100",
    "pf--Epson_SureColorT3100X",
    "pf--Epson_SureColorT5100",
}

_REFERENCE_DRIVER_400_HEAD_PACKAGES = {
    "pf--Epson_SureColorF500",
    "pf--Epson_SureColorT3100",
    "pf--Epson_SureColorT3100X",
    "pf--Epson_SureColorT5100",
}
_FOUR_COLOR_HEAD_OFFSETS_BY_SELECTOR = {0x00: 0, 0x02: 0, 0x01: 120, 0x04: 240}


def _direct_physical_channels(source: dict[str, Any]) -> list[dict[str, Any]] | None:
    driver_id = str((source.get("driver") or {}).get("id") or "")
    definition = _DIRECT_PHYSICAL_CHANNELS.get(driver_id)
    if definition is None:
        return None
    return [
        {"slot": slot, "role": role, "esc_i_code": selector}
        for slot, role, selector in definition
    ]


def _collapse_black_head_banks(
    channels: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Expose 0x41/0x42/0x44 as one ink while preserving all head banks."""

    bank_selectors = {0x41, 0x42, 0x44}
    banks = [
        item
        for item in channels
        if int(str(item.get("esc_i_code") or "0"), 0) in bank_selectors
    ]
    if {
        int(str(item.get("esc_i_code") or "0"), 0) for item in banks
    } != bank_selectors:
        return channels
    ordinary = [item for item in channels if item not in banks]
    black = {
        "slot": "k",
        "role": "black",
        "esc_i_code": "0x41",
        "additional_esc_i_codes": ("0x42", "0x44"),
        "head_offset": int(banks[0].get("head_offset") or 0),
    }
    return [black, *ordinary]


def _ink_short_name(role: str) -> str:
    """Return conventional photographic-ink labels used by printer UIs."""

    normalized = re.sub(r"[^a-z0-9]+", "_", role.casefold()).strip("_")
    aliases = {
        "black": "K",
        "photo_black": "PK",
        "photoblack": "PK",
        "ultrachrome_photo_black": "PK",
        "matte_black": "MK",
        "matteblack": "MK",
        "ultrachrome_matte_black": "MK",
        "pigment_black": "PGBK",
        "pigment_matte_black": "PGBK",
        "lightblack": "LK",
        "light_black": "LK",
        "gray": "LK",
        "grey": "LK",
        "lightlightblack": "LLK",
        "light_light_black": "LLK",
        "light_gray": "LLK",
        "light_grey": "LLK",
        "cyan": "C",
        "lightcyan": "LC",
        "light_cyan": "LC",
        "magenta": "M",
        "vivid_magenta": "VM",
        "lightmagenta": "LM",
        "light_magenta": "LM",
        "vivid_light_magenta": "VLM",
        "yellow": "Y",
        "red": "R",
        "orange": "O",
        "green": "G",
        "blue": "B",
        "violet": "V",
    }
    return aliases.get(normalized, role.upper())


def _raw_inksets(runtime: dict[str, Any]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in runtime.get("inksets", ()):
        grouped.setdefault(str(item.get("configuration_id") or "default"), []).append(item)
    selected: list[dict[str, Any]] = []
    for records in grouped.values():
        physical = [
            item for item in records
            if str(item.get("name") or "").casefold().startswith("physical")
        ]
        candidates = physical or records
        if not candidates:
            continue
        selected.append(
            max(candidates, key=lambda item: (len(item.get("channels") or ()), str(item.get("name"))))
        )
    return selected


def _ink_configurations(runtime: dict[str, Any]) -> tuple[InkConfigurationCapability, ...]:
    if not runtime.get("interchangeable_ink"):
        return ()
    result: list[InkConfigurationCapability] = []
    used: set[str] = set()
    for item in _raw_inksets(runtime):
        identity = _identifier(str(item.get("configuration_id") or "default"), "default")
        if identity in used:
            continue
        used.add(identity)
        result.append(
            InkConfigurationCapability(
                identity=identity,
                display_name=str(item.get("configuration_name") or identity),
                active_logical_ink=(
                    "PK"
                    if "photo" in str(item.get("configuration_name") or "").casefold()
                    else "MK"
                    if "matte" in str(item.get("configuration_name") or "").casefold()
                    else None
                ),
            )
        )
    return tuple(result)


def _physical_paths(
    package: CatalogPackage,
    source: dict[str, Any],
    runtime: dict[str, Any],
) -> tuple[PhysicalPathDefinition, ...]:
    driver_id = str((source.get("driver") or {}).get("id") or "")
    explicit_channels = _direct_physical_channels(source)
    channels = explicit_channels or list(source.get("channels") or ())
    configuration_roles: dict[int, list[tuple[str, str]]] = {}
    if (
        runtime.get("schema") == "pytone-rip.gutenprint-runtime"
        and explicit_channels is None
    ):
        raw_inksets = _raw_inksets(runtime)
        channels = []
        for inkset in raw_inksets[:1]:
            configuration_name = str(
                inkset.get("configuration_name") or ""
            ).casefold()
            selected_channels = list(inkset.get("channels", ()))
            for channel in selected_channels:
                role = str(channel.get("role") or channel.get("slot") or "unknown")
                selector = int(str(channel.get("esc_i_code", "0")), 0)
                if role.casefold() == "black" and "photo" in configuration_name:
                    role = "photo_black"
                elif role.casefold() == "black" and "matte" in configuration_name:
                    role = "matte_black"
                channels.append(
                    {**channel, "role": role, "inkset": inkset.get("name")}
                )
        if runtime.get("interchangeable_ink"):
            for inkset in raw_inksets:
                configuration_id = _identifier(
                    str(inkset.get("configuration_id") or "default"), "default"
                )
                configuration_name = str(
                    inkset.get("configuration_name") or configuration_id
                ).casefold()
                for channel in inkset.get("channels", ()):
                    selector = int(str(channel.get("esc_i_code", "0")), 0)
                    role = str(channel.get("role") or channel.get("slot") or "unknown")
                    if selector == 0x00 and "photo" in configuration_name:
                        role = "photo_black"
                    elif selector == 0x00 and "matte" in configuration_name:
                        role = "matte_black"
                    configuration_roles.setdefault(selector, []).append(
                        (configuration_id, role)
                    )
    if package.protocol_family == "P700RIP":
        channels = _p700_channels()
    elif package.package_id.startswith("gp--") or any(
        item.get("inkset") for item in channels
    ):
        channels = _choose_gutenprint_channels(channels)
    channels = _collapse_black_head_banks(channels)
    if package.package_id in _MATCHED_FOUR_COLOR_HEAD_OFFSETS:
        if driver_id != "XP305":
            raise ValueError(
                f"Matched head geometry requires ReferenceDriver XP305: {package.package_id}"
            )
        selectors = {int(str(item.get("esc_i_code") or "0"), 0) for item in channels}
        if selectors != set(_FOUR_COLOR_HEAD_OFFSETS_BY_SELECTOR):
            raise ValueError(
                f"Matched head offsets no longer fit {package.package_id}: "
                f"{sorted(selectors)}"
            )
        channels = [
            {
                **item,
                "head_offset": _FOUR_COLOR_HEAD_OFFSETS_BY_SELECTOR[
                    int(str(item["esc_i_code"]), 0)
                ],
            }
            for item in channels
        ]
    if not channels:
        raise ValueError(
            f"Imported direct-channel printer has no ESC i channel table: "
            f"{package.package_id}"
        )
    result: list[PhysicalPathDefinition] = []
    for index, item in enumerate(channels):
        role = str(item.get("role") or item.get("slot") or "UNKNOWN")
        selector = int(str(item.get("esc_i_code") or "0"), 0)
        additional_selectors = tuple(
            int(str(value), 0) for value in item.get("additional_esc_i_codes", ())
        )
        roles = tuple(configuration_roles.get(selector, ()))
        short_names = tuple(dict.fromkeys(_ink_short_name(value) for _, value in roles))
        short_name = "/".join(short_names) if short_names else _ink_short_name(role)
        default_logical_ink: str | None = None
        if driver_id in {"XP510", "XP800"}:
            # These are two independently addressable cartridges, not the
            # interchangeable PK/MK slot used by P600/P800.  QTR must be able
            # to drive both simultaneously: pigment BK is K and photo BK is
            # exposed as the fifth curve LK.
            if selector == 0x00:
                short_name = "BK"
                default_logical_ink = "K"
            elif selector == 0x40:
                short_name = "PBK"
                default_logical_ink = "LK"
        elif driver_id == "ET8550":
            short_name = {"pigment_matte_black": "PGBK", "photo_black": "PBK", "gray": "GY"}.get(
                role, short_name
            )
            default_logical_ink = {"pigment_matte_black": "K", "photo_black": "LK", "gray": "LLK"}.get(
                role
            )
        result.append(
            PhysicalPathDefinition(
            identity=PhysicalPathId(f"P{index}"),
            factory_role=role,
            escp2_selector=selector,
            head_offset=int(item.get("head_offset") or 0),
            allocation_index=index,
            media_classes=tuple(str(value) for value in item.get("media", ())),
            short_name=short_name,
            display_name=f"{short_name} — {role.replace('_', ' ').title()}",
            configuration_roles=roles,
            additional_escp2_selectors=additional_selectors,
            default_logical_ink=default_logical_ink,
        )
        )
    return tuple(result)


def _vendor_modes(
    source: dict[str, Any], runtime: dict[str, Any], rows_per_pass: int,
    *, protocol_family: str,
) -> tuple[PrinterMode, ...]:
    if runtime.get("schema") != "pytone-rip.mode-runtime-v2":
        raise ValueError(
            "Direct ESC/P2 mode data must use the documented mode-runtime-v2 schema"
        )
    records = list(runtime.get("modes") or ())
    result: list[PrinterMode] = []
    used: set[str] = set()
    for index, item in enumerate(records):
        app_dpi = item.get("app_raster_dpi") or {}
        printer_dpi = item.get("printer_coordinate_dpi") or {}
        application = (int(app_dpi["horizontal"]), int(app_dpi["vertical"]))
        physical = (
            int(printer_dpi["horizontal"]),
            int(printer_dpi["vertical"]),
        )
        applicability = item.get("applicability") or {}
        paper_token = (
            "X"
            if applicability.get("media_scope") == "all_non_default_media"
            else str(int(applicability.get("media_index", 0)))
        )
        available_media_indices = {
            int(media.get("idx", media_index))
            for media_index, media in enumerate(source.get("media") or ())
        }
        if paper_token.isdecimal() and int(paper_token) not in available_media_indices:
            # Some source descriptors retain private modes for a media entry
            # that is not present in the public model table. Advertising that
            # mode would create an unselectable and mechanically undefined
            # media/mode pair, so it stays out of the normalized model.
            continue
        mode_id = _identifier(
            f"{item.get('display_name')}_{application[0]}x{application[1]}_paper_{paper_token}",
            f"mode_{index}",
        )
        if mode_id in used:
            continue
        used.add(mode_id)
        escp2 = item.get("escp2") or {}
        weave = escp2.get("software_weave") or {}
        flags = escp2.get("direction_and_layout") or {}
        print_method = escp2.get("print_method") or {}
        explicit_weave = tuple(
            (
                int(entry["relative_feed_units"]),
                int(entry["nozzle_phase"]),
                int(entry["pass_in_phase"]),
            )
            for entry in item.get("explicit_weave_sequence") or ()
        )
        internal_flags = (
            (0x02 if flags.get("internal_layout_flag_1") else 0)
            | (0x08 if flags.get("internal_layout_flag_3") else 0)
            | int(flags.get("unknown_high_bits") or 0)
        )
        base = min(application[0], physical[0], 720)
        # ESC (D defines the horizontal spacing of dots in each ESC i row.
        # The host must interleave enough rows to cover the application raster;
        # using a coarser inferred base doubled ET-2550's printed A6 width.
        printhead = ESCP2_FAMILY_PROFILES[protocol_family].printhead_resolution_payload
        head_scale = int.from_bytes(printhead[:2], "little")
        horizontal_unit = printhead[3]
        if not horizontal_unit or head_scale % horizontal_unit:
            raise ValueError(f"{protocol_family} has an invalid ESC (D horizontal grid")
        head_dpi = head_scale // horizontal_unit
        # Some imported modes (notably ET-14000's 360/720 dpi modes on the
        # WF7610 profile) are not integer multiples of the family grid. Their
        # resampling/command framing needs a separate model-specific audit.
        if application[0] % head_dpi == 0:
            base = min(base, head_dpi)
        horizontal_passes = max(1, application[0] // max(1, base))
        vertical_passes = max(1, physical[1] // max(1, application[1]))
        quality_name = str(item.get("display_name") or mode_id)
        # ReferenceDriver's Description names the advertised quality. PrinterDPI is
        # the command coordinate grid and must stay unchanged for ESC/P2.
        nominal = item.get("nominal_output_dpi") or {}
        output_resolution = (
            int(nominal.get("horizontal") or physical[0]),
            int(nominal.get("vertical") or physical[1]),
        )
        levels = tuple(int(value) for value in item.get("drop_levels_percent") or ())
        if levels and (
            len(levels) < 2 or levels[0] != 0 or levels[-1] != 100
            or any(left >= right for left, right in zip(levels, levels[1:]))
        ):
            raise ValueError(f"Invalid ReferenceDriver drop levels in {mode_id}: {levels}")
        drop_sizes = tuple(value / 100.0 for value in levels[1:]) if levels else (0.4, 0.6, 1.0)
        bit_depth = int(item.get("raster_bit_depth") or 2)
        if len(drop_sizes) > (1 << bit_depth) - 1:
            raise ValueError(f"ReferenceDriver drop levels exceed raster bit depth in {mode_id}")
        mode_resolution = f"{output_resolution[0]} x {output_resolution[1]} DPI"
        if output_resolution != application:
            mode_display_name = (
                f"{quality_name} — {mode_resolution} "
                f"(raster {application[0]} x {application[1]} DPI)"
            )
        else:
            mode_display_name = f"{quality_name} — {mode_resolution}"
        result.append(
            PrinterMode(
                identity=mode_id,
                # The user chooses a printer quality mode here, not the
                # Windows/XPS rasterization resolution. Several Epson modes
                # deliberately consume the same application raster while
                # changing native coordinates and pass count (for example,
                # ET-8550 Super/Photo are both 1440x1440 input but print at
                # 1440/2880/5760 horizontal printer DPI depending on media).
                # Showing only application DPI made distinct modes look like
                # duplicates and hid the printer's highest quality modes.
                display_name=mode_display_name,
                horizontal_dpi=application[0],
                vertical_dpi=application[1],
                physical_horizontal_dpi=physical[0],
                physical_vertical_dpi=physical[1],
                output_resolution_dpi=output_resolution,
                base_resolution_dpi=max(1, base),
                horizontal_passes=horizontal_passes,
                vertical_passes=vertical_passes,
                nozzle_count=rows_per_pass,
                nozzle_separation=2,
                bit_depth=bit_depth,
                dot_size=int(escp2.get("variable_dot_mode") or 0),
                drop_sizes=drop_sizes,
                density=1.0,
                source="documented Epson direct-raster mode parameters",
                implemented=True,
                offline_validated=True,
                ordered_matrix_id=(
                    f"matrix-{application[0] // application[1]}x1"
                    if application[0] // application[1] in (1, 2, 4)
                    else "matrix-1x1"
                ),
                escp2_dot_size_override=int(escp2["variable_dot_mode"]),
                escp2_print_method_override=int(print_method["default"]),
                escp2_alternate_print_method=(
                    int(print_method["precision_variant"])
                    if "precision_variant" in print_method
                    else None
                ),
                escp2_default_direction=str(
                    flags.get("default_direction") or "bidirectional"
                ),
                escp2_emit_legacy_s_mode_2=bool(
                    flags.get("emit_legacy_esc_s_mode_2")
                ),
                escp2_internal_layout_flags=internal_flags,
                escp2_weave_pass_group_count=max(
                    1, int(weave.get("pass_group_count") or 1)
                ),
                escp2_weave_variant_code=int(
                    weave.get("variant_code")
                    or weave.get("legacy_layout_code")
                    or 0
                ),
                escp2_explicit_weave_sequence=explicit_weave,
                media_class=f"pf:{paper_token}",
            )
        )
    return tuple(result)


def _gutenprint_modes(
    source: dict[str, Any], runtime: dict[str, Any]
) -> tuple[PrinterMode, ...]:
    if runtime.get("schema") == "pytone-rip.gutenprint-runtime":
        result: list[PrinterMode] = []
        for index, item in enumerate(runtime.get("modes", ())):
            # Gutenprint multiplies the job's ink density by this mode value.
            # An explicit zero produces no printable ink; it is not a usable
            # extra resolution merely because it appears in the model XML.
            if item.get("density") is not None and float(item["density"]) <= 0:
                continue
            horizontal = int(item.get("horizontal_dpi") or 0)
            vertical = int(item.get("vertical_dpi") or 0)
            physical_horizontal = int(item.get("physical_horizontal_dpi") or horizontal)
            physical_vertical = int(item.get("physical_vertical_dpi") or vertical)
            if min(horizontal, vertical, physical_horizontal, physical_vertical) <= 0:
                continue
            ratio = horizontal // vertical if vertical else 1
            result.append(
                PrinterMode(
                    identity=str(item.get("identity") or f"mode_{index}"),
                    display_name=str(
                        item.get("display_name") or item.get("identity") or f"Mode {index + 1}"
                    ),
                    horizontal_dpi=horizontal,
                    vertical_dpi=vertical,
                    physical_horizontal_dpi=physical_horizontal,
                    physical_vertical_dpi=physical_vertical,
                    output_resolution_dpi=(horizontal, vertical),
                    base_resolution_dpi=max(1, int(item.get("base_resolution_dpi") or 360)),
                    horizontal_passes=max(1, int(item.get("horizontal_passes") or 1)),
                    vertical_passes=max(1, int(item.get("vertical_passes") or 1)),
                    nozzle_count=max(1, int(item.get("nozzle_count") or 1)),
                    nozzle_separation=max(1, int(item.get("nozzle_separation") or 1)),
                    bit_depth=max(1, int(item.get("bit_depth") or 1)),
                    dot_size=int(item.get("dot_size") or 0),
                    escp2_printer_weave_mode=(
                        int(item["printer_weave_mode"])
                        if item.get("printer_weave_mode") is not None
                        else None
                    ),
                    drop_sizes=tuple(float(value) for value in item.get("drop_sizes", ())) or (1.0,),
                    density=float(item.get("density") or 1.0),
                    source="Gutenprint ESC/P2 model data",
                    implemented=True,
                    offline_validated=True,
                    ordered_matrix_id=(
                        f"matrix-{ratio}x1" if ratio in (1, 2, 4) else "matrix-1x1"
                    ),
                    media_class="generic",
                )
            )
        return tuple(result)

    model = runtime.get("model_xml") or {}
    records = list(model.get("resolutions") or source.get("resolutions") or ())
    heads = list(model.get("heads") or ())
    head = next((item for item in heads if item.get("type") == "default"), heads[0] if heads else {})
    nozzle_count = int(head.get("Nozzles") or 180)
    nozzle_separation = int(head.get("NozzleSeparation") or 2)
    result: list[PrinterMode] = []
    for index, item in enumerate(records):
        params = item.get("params") or {}
        mode_density = params.get("escp2_density", item.get("density"))
        if mode_density is not None and float(mode_density) <= 0:
            continue
        physical = _pair(item.get("physical") or item.get("physical_dpi"), (360, 360))
        printed = _pair(item.get("printed") or item.get("printed_dpi"), physical)
        base = int(params.get("escp2_base_res") or min(physical[0], 720))
        ratio = printed[0] // printed[1] if printed[1] else 1
        drops = tuple(
            float(params[name])
            for name in ("DropSize1", "DropSize2", "DropSize3")
            if params.get(name) is not None
        ) or tuple(float(value) for value in item.get("dropsizes", ()) if value is not None)
        result.append(
            PrinterMode(
                identity=str(item.get("name") or f"mode_{index}"),
                display_name=str(item.get("text") or item.get("name") or f"Mode {index + 1}"),
                horizontal_dpi=printed[0],
                vertical_dpi=printed[1],
                physical_horizontal_dpi=physical[0],
                physical_vertical_dpi=physical[1],
                output_resolution_dpi=printed,
                base_resolution_dpi=max(1, base),
                horizontal_passes=max(1, printed[0] // max(1, min(base, physical[0]))),
                vertical_passes=max(1, int(item.get("verticalPasses") or 1)),
                nozzle_count=nozzle_count,
                nozzle_separation=nozzle_separation,
                bit_depth=int(params.get("escp2_bits") or item.get("bits") or 2),
                dot_size=int(params.get("escp2_ink_type") or item.get("ink_type") or 0),
                drop_sizes=drops or (1.0,),
                density=float(params.get("escp2_density") or item.get("density") or 1.0),
                source="Gutenprint exported model XML",
                implemented=True,
                offline_validated=True,
                ordered_matrix_id=f"matrix-{ratio}x1" if ratio in (1, 2, 4) else "matrix-1x1",
                media_class="generic",
            )
        )
    return tuple(result)


_GUTENPRINT_MODEL_MAX_WIDTH_POINTS = {
    # Gutenprint 5.3.5 model XML maximumMediaSize. These are used only by
    # legacy catalog packages created before the field was persisted into the
    # resolved runtime JSON.
    "24": 1224,   # Stylus Color 3000, 17 inch
    "48": 1728,   # Stylus Pro 7600, 24 inch
    "49": 3168,   # Stylus Pro 9600, 44 inch
    "88": 4608,   # SureColor P20000, 64 inch
    "89": 1728,   # Stylus Pro 7880/7890 and SureColor P6000, 24 inch
    "90": 3168,   # Stylus Pro 9880/9890 and SureColor P10000/P8000, 44 inch
    "102": 1224,  # Stylus Pro 4880, 17 inch
    "103": 1728,  # Stylus Pro 7800, 24 inch
    "104": 3168,  # Stylus Pro 9800, 44 inch
    "106": 1224,  # Stylus Pro 4800, 17 inch
    "107": 1224,  # Stylus Pro 3880, 17 inch
    "108": 1224,  # Stylus Pro 3800, 17 inch
    "114": 1224,  # Stylus Pro 4000, 17 inch
    "116": 1728,  # Stylus Pro 7900, 24 inch
    "117": 3168,  # Stylus Pro 9900, 44 inch
    "124": 1728,  # SureColor P7000, 24 inch
    "125": 3168,  # SureColor P9000, 44 inch
    "126": 1728,  # SureColor P7000 Commercial, 24 inch
    "127": 3168,  # SureColor P9000 Commercial, 44 inch
    "131": 1224,  # Stylus Pro 4900, 17 inch
    "137": 1224,  # SureColor P5000 Standard, 17 inch
    "138": 1224,  # SureColor P5000 Commercial, 17 inch
}

# Standard large-format sheets exposed by ReferenceDriver's wide-format table and
# cross-checked against Gutenprint's papers/standard.xml. Dimensions are points.
# A model receives only entries whose portrait width fits maximumMediaSize;
# this applies equally to Stylus Pro, SureColor and other wide-format families.
_STANDARD_WIDE_PAGES = (
    ("A3", "A3", 842.0, 1191.0, 62),
    ("A3Plus", "A3+ / US B+", 936.0, 1368.0, 61),
    ("A2", "A2", 1191.0, 1684.0, 67),
    ("A1", "A1", 1684.0, 2384.0, 99),
    ("SuperA1", "Super A1 (24 x 36 in)", 1728.0, 2592.0, 99),
    ("A0", "A0", 2384.0, 3370.0, 99),
    ("SuperA0", "Super A0", 2591.0, 3662.0, 99),
    ("B4", "B4", 709.0, 1001.0, 63),
    ("B3", "B3", 1001.0, 1417.0, 66),
    ("B2", "B2", 1459.0, 2063.0, 99),
    ("B1", "B1", 2063.0, 2919.0, 99),
    ("B0", "B0", 2919.0, 4127.0, 99),
    ("Tabloid", "US B (11 x 17 in)", 792.0, 1224.0, 64),
    ("EngineeringC", "US C (17 x 22 in)", 1224.0, 1584.0, 68),
    ("USD", "US D (22 x 34 in)", 1584.0, 2448.0, 99),
    ("USE", "US E (34 x 44 in)", 2448.0, 3168.0, 99),
    ("ArchC", "Arch C (18 x 24 in)", 1296.0, 1728.0, 99),
    ("ArchD", "Arch D (24 x 36 in)", 1728.0, 2592.0, 99),
    ("ArchE", "Arch E (36 x 48 in)", 2592.0, 3456.0, 99),
    ("11x14", "11 x 14 in", 792.0, 1008.0, 65),
    ("16x20", "16 x 20 in", 1152.0, 1440.0, 99),
    ("30x40cm", "30 x 40 cm", 850.4, 1133.9, 99),
    ("40x60cm", "40 x 60 cm", 1133.9, 1700.8, 99),
    ("44x36", "44 x 36 in", 3168.0, 2592.0, 99),
)

# These are exact model matches between the bundled Gutenprint and ReferenceDriver
# descriptors. Use the ReferenceDriver paper selectors where the Gutenprint model has
# no PaperMedia value; keep Gutenprint's own value whenever it is present.
_REFERENCE_DRIVER_PAPER_PEERS = {
    "gp--escp2-et2550": "pf--Epson_EcoTank_ET2550",
    "gp--escp2-et2600": "pf--Epson_EcoTank_ET2600",
    "gp--escp2-et2650": "pf--Epson_EcoTank_ET2650",
    "gp--escp2-et2700": "pf--Epson_EcoTank_ET2700",
    "gp--escp2-et2750": "pf--Epson_EcoTank_ET2750",
    "gp--escp2-xp240": "pf--Epson_XP240series",
    "gp--escp2-xp340": "pf--Epson_XP340series",
    "gp--escp2-xp440": "pf--Epson_XP440series",
}

_GUTENPRINT_TO_REFERENCE_DRIVER_PAPER = {
    "Plain": "Plain paper",
    "MatteHeavy": "Epson Matte Heavy",
    "GlossyPhoto": "Epson Photo Glossy",
    "Photo": "Epson Photo Glossy",
}

# Both installed Epson_SureColorP700/P900 (RGB) descriptors address the same
# REMOTE1 media selector as the direct-channel *_RIP descriptors. Their A4
# ReferenceDriver PRNs confirm these nine additional MI bytes on both models. The
# RIP descriptor omits them from its paper menu, so borrow only the selector;
# use an existing RIP paper class for the native ESC i mode mechanics.
_P700_P900_ADDITIONAL_MEDIA = (
    ("Epson Photo Glossy", 43, "Epson Photo Glossy", "photo"),
    ("Baryta", 174, "Epson Premium Luster", "photo"),
    ("Epson Proofing Paper", 142, "Epson Proof Paper", "photo"),
    ("Velvet Fineart", 25, "Archival Matte", "matte"),
    ("UltraSmooth Fineart", 39, "Archival Matte", "matte"),
    ("Watercolor Radiant White", 16, "Archival Matte", "matte"),
    ("Fineart Matte", 206, "Archival Matte", "matte"),
    ("Canvas Satin", 171, "Epson Premium Luster", "photo"),
    ("Canvas Matte", 169, "Archival Matte", "matte"),
)

# R2880's Epson ESC/P-R PPD names the physical sheets but supplies no ESC/P2
# MI size byte. These pairs are from its installed ReferenceDriver TPP and verified
# A4, 4x6, and 13x19 PRNs. Unknown PPD sizes keep the generic code 99.
_R2880_PAGE_SIZES = {
    "L": (15, 3500, 5000), "TL": (15, 3500, 5000),
    "4X6FULL": (10, 4000, 6000), "T4X6FULL": (10, 4000, 6000),
    "2L": (28, 5000, 7000), "T2L": (28, 5000, 7000),
    "8x10": (12, 8000, 10000), "T8x10": (12, 8000, 10000),
    "A4": (0, 8268, 11693), "TA4": (0, 8268, 11693),
    "Letter": (1, 8500, 11000), "TLetter": (1, 8500, 11000),
    "USB": (64, 11000, 17000), "TUSB": (64, 11000, 17000),
    "A3": (62, 11693, 16535), "TA3": (62, 11693, 16535),
    "A3+": (61, 13000, 19000), "TA3+": (61, 13000, 19000),
}

# The R3000 product descriptor assigns the same MI size bytes to these
# physical formats. Its captured Letter job confirms byte 1 independently.
_R3000_PAGE_SIZES = _R2880_PAGE_SIZES

# Gutenprint model 115 -> media/f360_ultrachrome_k3v.xml, Density under each
# paper's ink configuration. Photo and matte black carry the same value here.
_R3000_PAPER_DENSITY = {
    "Plain": 0.72, "Postcard": 0.72, "Envelope": 0.72, "Inkjet": 0.72,
    "GlossyFilm": 0.83, "Transparency": 0.83, "BackFilm": 0.83,
    "Matte": 0.92, "MatteHeavy": 0.92, "Coated": 0.83,
    "Photo": 1.0, "GlossyPhoto": 0.72,
    "Semigloss": 0.72, "Luster": 0.72,
    "ArchivalMatte": 0.92, "WaterColorRadiant": 0.92,
    "GlossyPaper": 0.83, "Ilford": 0.83,
}

# P800 model 130 uses the same f360_ultrachrome_k3v paper table. These are
# scalar coverage limits, equal for ultra3photo/ultra3matte, not tone curves.
# Key by MI because product aliases replace Gutenprint names during merging.
_P800_PAPER_DENSITY_BY_MI = {
    0: 0.72, 1: 0.72, 3: 0.92, 5: 0.92, 6: 1.0, 7: 0.83,
    9: 0.83, 10: 0.83, 11: 0.72, 12: 0.72, 13: 0.72,
    15: 0.92, 19: 0.83, 23: 0.83, 29: 0.72,
}


def _pages(
    source: dict[str, Any],
    ppd: PpdCapabilities,
    runtime: dict[str, Any],
    *,
    match_size_by_dimensions: bool = False,
    allow_synthetic_wide_pages: bool = False,
    trust_ppd_imageable_area: bool = True,
    include_unmatched_native_pages: bool = False,
) -> tuple[PageCapability, ...]:
    raw_sizes = list(source.get("media_sizes") or ())
    by_name = {
        _identifier(str(item.get("name") or ""), "").casefold(): item
        for item in raw_sizes
    }

    def page_fields(
        item: dict[str, Any] | None,
    ) -> tuple[int, str, int | None, tuple[int, int, int] | None, tuple[float, float] | None]:
        mechanics = (item or {}).get("page_mechanics") or {}
        special = mechanics.get("special_media") or {}
        native_format = (
            (
                int(item["width_mils"]),
                int(item["height_mils"]),
                int(mechanics["format_bottom_margin_mils"]),
            )
            if item is not None and "format_bottom_margin_mils" in mechanics
            else None
        )
        physical_size = None
        driver_id = (source.get("driver") or {}).get("id")
        source_name = str((source.get("identity") or {}).get("name") or "")
        if (
            not trust_ppd_imageable_area
            and item is not None
            and (driver_id in {"P600", "P800"} or (driver_id == "P700" and source_name.endswith("_RIP")))
        ):
            # Verified against ReferenceDriver descriptors and Windows capabilities
            # for P600/P700/P800/P900:
            # TPP Size includes 100 mil on both horizontal edges and 150 mil
            # on both vertical edges for borderless modes. Preserve that canvas;
            # publish the actual sheet size to Windows and the settings GUI.
            borderless = "borderless" in str(item.get("name") or "").casefold()
            width = int(item["width_mils"]) - (200 if borderless else 0)
            height = int(item["height_mils"]) - (300 if borderless else 0)
            if width <= 0 or height <= 0:
                raise ValueError("Invalid ReferenceDriver borderless physical media dimensions")
            # ReferenceDriver's Windows forms use PPD points rounded to two decimals;
            # e.g. 8268 mil -> 595.30 pt -> 210009 microns. The synthetic PPD
            # truncated this to 595 pt, producing the old 209903 micron variant.
            physical_size = (
                round(width * 72 / 1000, 2) * 25.4 / 72,
                round(height * 72 / 1000, 2) * 25.4 / 72,
            )
        return (
            int(mechanics.get("media_size_code", 99)),
            str(mechanics.get("sheet_geometry_class") or "standard_sheet"),
            int(special["selector"]) if "selector" in special else None,
            native_format,
            physical_size,
        )

    if ppd.pages:
        result: list[PageCapability] = []
        source_name = str((source.get("identity") or {}).get("name") or "")
        t_series_sheet_bottom_mm = (
            20.0 if source_name == "Epson_SureColorT5400"
            else 14.0 if source_name in {
                "Epson_SureColorT3200",
                "Epson_SureColorT5200",
                "Epson_SureColorT7200",
            }
            else None
        )
        gutenprint_driver = (source.get("identity") or {}).get("gutenprint_driver")
        r2880 = gutenprint_driver == "escp2-r2880"
        r3000 = gutenprint_driver == "escp2-r3000"
        p700_roll_width = {
            "Epson_SureColorP700_RIP": (13000, 19000),
            "Epson_SureColorP900_RIP": (17000, 22000),
            "Epson_SureColorP800": (17000, 22000),
        }.get(str((source.get("identity") or {}).get("name") or ""))
        for item in ppd.pages:
            # A PPD's Custom entry describes a variable-size range. The
            # synthetic ReferenceDriver PPD encodes its maximum as one fixed page;
            # advertising that as a selectable format produces a bogus
            # multi-metre sheet in Windows Page Setup.
            if (
                not trust_ppd_imageable_area
                and item.label.strip('"').strip().casefold() == "custom"
            ):
                continue
            if p700_roll_width and "cd" in item.name.casefold():
                # A printable disc is not a sheet or roll page in this RIP.
                continue
            page = item
            if (
                not trust_ppd_imageable_area
                and item.borderless
                and "borderless" not in item.label.casefold()
            ):
                # The synthetic ReferenceDriver PPD writes a full ImageableArea for
                # every page, including plain A4 and envelopes. It does not
                # establish edge-to-edge capability for those page choices.
                margin = min(9.0, item.width_points / 20, item.height_points / 20)
                page = replace(
                    item,
                    imageable_left_points=margin,
                    imageable_bottom_points=margin,
                    imageable_right_points=item.width_points - margin,
                    imageable_top_points=item.height_points - margin,
                )
            descriptor = by_name.get(
                _identifier(item.label, "").casefold(),
                by_name.get(_identifier(item.name, "").casefold()),
            )
            et_driver = (source.get("driver") or {}).get("id") == "ET8550"
            et_borderless = et_driver and item.borderless
            tolerance_points = 5 if et_driver else 1
            if descriptor is not None and et_driver:
                # A3+ normalizes to "A3" as an identifier. A name collision
                # must not send A3's MI size code to a 13x19-inch sheet.
                if (
                    abs(
                        (float(descriptor.get("width_mils", 0)) - (200 if et_borderless else 0))
                        * 0.072 - item.width_points
                    ) >= tolerance_points
                    or abs(
                        (float(descriptor.get("height_mils", 0)) - (300 if et_borderless else 0))
                        * 0.072 - item.height_points
                    ) >= tolerance_points
                ):
                    descriptor = None
            if descriptor is None and (
                match_size_by_dimensions or et_driver
            ):
                # ET-8500/8550 ReferenceDriver borderless Size entries describe an
                # enlarged canvas (200 mil wider, 300 mil taller) while the
                # Epson PPD describes the physical sheet. Match that sheet to
                # retain the actual MI size byte, including 4x6 = 10.
                matching = [
                    size
                    for size in raw_sizes
                    if (
                        not et_borderless
                        or "borderless" in str(size.get("name") or "").casefold()
                    )
                    and abs(
                        (float(size.get("width_mils", 0)) - (200 if et_borderless else 0))
                        * 0.072 - item.width_points
                    ) < tolerance_points
                    and abs(
                        (float(size.get("height_mils", 0)) - (300 if et_borderless else 0))
                        * 0.072 - item.height_points
                    ) < tolerance_points
                ]
                codes = {
                    int((size.get("page_mechanics") or {}).get("media_size_code", 99))
                    for size in matching
                }
                if len(codes) == 1:
                    descriptor = matching[0]
            media_size_code, geometry_class, special_selector, native_format, physical_size = page_fields(descriptor)
            if et_driver and descriptor is not None:
                # Exact command canvas from the descriptor; Windows keeps its
                # physical sheet dimensions. Borderless descriptors include
                # the driver's extra 200/300 mil canvas, not a larger sheet.
                width_mils = int(descriptor["width_mils"]) - (200 if page.borderless else 0)
                height_mils = int(descriptor["height_mils"]) - (300 if page.borderless else 0)
                native_format = (width_mils, height_mils, 120)
            if (
                t_series_sheet_bottom_mm is not None
                and geometry_class != "roll"
                and not page.borderless
            ):
                # The synthetic PPD advertises the full sheet. Epson specifies
                # a larger non-printable bottom edge for these cut-sheet models.
                bottom = max(
                    page.imageable_bottom_points,
                    t_series_sheet_bottom_mm * 72.0 / 25.4,
                )
                page = replace(page, imageable_bottom_points=bottom)
            if (r2880 or r3000) and page.name in _R3000_PAGE_SIZES:
                media_size_code, width_mils, height_mils = _R3000_PAGE_SIZES[page.name]
                native_format = (width_mils, height_mils, 60 if r3000 else 120)
                physical_size = (width_mils * 25.4 / 1000, height_mils * 25.4 / 1000)
            if p700_roll_width and descriptor is not None and not page.borderless:
                # ReferenceDriver's source dimensions are in mils. Rounded PPD points
                # otherwise add 1-2 native units to A4 and other sheet limits.
                native_format = (
                    int(descriptor["width_mils"]),
                    int(descriptor["height_mils"]),
                    120,
                )
            if source_name == "Epson_SureColorP700_RIP" and page.name == "A5":
                # The imported descriptor's 5846 mil width becomes 421 PPD
                # points, but Windows ISOA5 is the physical 148 x 210 mm sheet.
                # Keep the audited device page format and raster canvas separate.
                physical_size = (148.0, 210.0)
            if source_name == "Epson_SureColorP800" and page.label == "A2":
                physical_size = (420.0, 594.0)
            result.append(
                PageCapability(
                    page,
                    True,
                    True,
                    media_size_code=media_size_code,
                    sheet_geometry_class=geometry_class,
                    special_media_selector=special_selector,
                    native_page_format_mils=native_format,
                    physical_media_size_mm=physical_size,
                )
            )
        if include_unmatched_native_pages:
            # The ET-2550 vendor PPD omits fixed formats declared by its exact
            # ET-2500/2550 descriptor, including Panorama. Keep the PPD's
            # existing identities and margins; add only distinct sheet sizes.
            margin_page = next(p.ppd for p in result if p.ppd.name == "A4")
            for descriptor in raw_sizes:
                label = str(descriptor.get("name") or "")
                if not label or label.casefold() == "custom":
                    continue
                width_mm = float(descriptor["width_mils"]) * 25.4 / 1000
                height_mm = float(descriptor["height_mils"]) * 25.4 / 1000
                name = _identifier(label, "Page")
                named_page = next((p for p in result if p.ppd.name == name), None)
                if named_page is not None:
                    if (abs(named_page.media_width_mm - width_mm) < 0.6
                            and abs(named_page.media_height_mm - height_mm) < 0.6):
                        continue
                    raise ValueError(f"Conflicting native page identity: {name}")
                if any(not p.ppd.borderless
                       and abs(p.media_width_mm - width_mm) < 0.354
                       and abs(p.media_height_mm - height_mm) < 0.354
                       for p in result):
                    continue
                width_pt, height_pt = width_mm * 72 / 25.4, height_mm * 72 / 25.4
                size_code, geometry, selector, native_format, _ = page_fields(descriptor)
                # Captured ET-2500/2550 streams use a 120 mil bottom limit
                # for sheets and 540 mil for these two envelope formats.
                bottom_mils = 540 if geometry == "envelope" else 120
                native_format = (int(descriptor["width_mils"]),
                                 int(descriptor["height_mils"]), bottom_mils)
                result.append(PageCapability(
                    PpdPage(name, label, width_pt, height_pt,
                            margin_page.imageable_left_points,
                            bottom_mils * 72 / 1000,
                            width_pt - (margin_page.width_points - margin_page.imageable_right_points),
                            height_pt - (margin_page.height_points - margin_page.imageable_top_points)),
                    True, True, media_size_code=size_code,
                    sheet_geometry_class=geometry, special_media_selector=selector,
                    native_page_format_mils=native_format,
                    physical_media_size_mm=(width_mm, height_mm),
                ))
        # Some Epson PPDs list only regional/photo presets. The exact model's
        # imported descriptor still declares A5/A6; do not discard those sheet
        # capabilities just because the PPD omits their named presets.
        # R800/2000P additionally have these sheets in their audited product
        # descriptors, but their legacy tables supply no MI size selectors.
        for name, width_mm, height_mm in (("A5", 148.0, 210.0), ("A6", 105.0, 148.0)):
            descriptor = next((s for s in raw_sizes if s.get("name") == name), None)
            if descriptor is None and gutenprint_driver not in {"escp2-r800", "escp2-2000"}:
                continue
            if any(p.ppd.name == name or (
                not p.ppd.borderless and abs(p.media_width_mm - width_mm) < 0.6
                and abs(p.media_height_mm - height_mm) < 0.6
            ) for p in result):
                continue
            mechanics = (descriptor or {}).get("page_mechanics") or {}
            size_code = int(mechanics.get("media_size_code",
                (descriptor or {}).get("paper_media_size_code", 99)))
            width_pt, height_pt = width_mm * 72 / 25.4, height_mm * 72 / 25.4
            result.append(PageCapability(
                PpdPage(name, name, width_pt, height_pt, 9.0, 9.0,
                        width_pt - 9.0, height_pt - 9.0),
                True, True, media_size_code=size_code,
                physical_media_size_mm=(width_mm, height_mm),
            ))
        maximum = _pair(runtime.get("maximum_media_size_points"), (0, 0))[0]
        if maximum <= 0:
            maximum = _GUTENPRINT_MODEL_MAX_WIDTH_POINTS.get(
                str(runtime.get("model_id") or ""), 0
            )
        # An Epson vendor PPD is the authority for supported sheet formats.
        # Only incomplete synthetic PPDs may need the wide-format fallback.
        if allow_synthetic_wide_pages and maximum > 612:
            existing = {page.ppd.name.casefold() for page in result}
            for name, label, width, height, media_size_code in _STANDARD_WIDE_PAGES:
                if width > maximum or name.casefold() in existing:
                    continue
                result.append(PageCapability(
                    PpdPage(name, label, width, height, 9.0, 9.0, width - 9.0, height - 9.0),
                    True,
                    True,
                    media_size_code=media_size_code,
                    sheet_geometry_class="standard_sheet",
                ))
                existing.add(name.casefold())
        if p700_roll_width:
            width_mils, height_mils = p700_roll_width
            width_in, height_in = width_mils // 1000, height_mils // 1000
            # The same physical 13×19 / 17×22 size is already named in the
            # ReferenceDriver descriptor; roll feed changes PP and page margins, not
            # the MI size byte (61 / 68 in captured roll PRNs).
            roll_size_code = 61 if width_in == 13 else 68
            result.append(PageCapability(
                PpdPage(
                    f"Roll{width_in}x{height_in}",
                    f"{width_in} × {height_in} in (roll)",
                    width_in * 72.0, height_in * 72.0,
                    9.0, 9.0, width_in * 72.0 - 9.0,
                    height_in * 72.0 - 9.0,
                ),
                True, True, media_size_code=roll_size_code,
                sheet_geometry_class="roll",
                native_page_format_mils=(width_mils, height_mils, 120),
            ))
        if source_name == "Epson_SureColorP800" or r2880 or r3000 or p700_roll_width:
            # Epson's P800 paper specifications include A5/A6/JIS B5 and
            # smaller photo sheets omitted by its product descriptor. Size
            # bytes come from model 130's mediaSizes -> standard.xml.
            sizes = (
                ("A5", "A5", 148.0, 210.0, 3),
                ("A6", "A6", 105.0, 148.0, 4),
                ("B5", "B5", 182.0, 257.0, 5),
                ("4X6FULL", '4 × 6 in', 101.6, 152.4, 10),
                ("5x7", '5 × 7 in', 127.0, 177.8, 28),
                ("5x8", '5 × 8 in', 127.0, 203.2, 11),
                ("10x15cm", '10 × 15 cm', 100.0, 150.0, 13),
                ("L", 'L (3.5 × 5 in)', 88.9, 127.0, 15),
                ("B4", 'B4 (JIS)', 257.0, 364.0, 63),
                ("Legal", 'Legal', 215.9, 355.6, 2),
                ("HalfLetter", 'Half Letter', 139.7, 215.9, 7),
                ("11x14", '11 × 14 in', 279.4, 355.6, 65),
                ("12x12", '12 × 12 in', 304.8, 304.8, 99),
            )
            for name, label, width_mm, height_mm, size_code in sizes:
                if source_name == "Epson_SureColorP800" and name in {
                    "B4", "Legal", "HalfLetter", "11x14", "12x12",
                }:
                    continue
                if any(p.ppd.name == name or (
                    not p.ppd.borderless and abs(p.media_width_mm - width_mm) < 0.02
                    and abs(p.media_height_mm - height_mm) < 0.02
                ) for p in result):
                    continue
                if source_name in {"Epson_SureColorP700_RIP", "Epson_SureColorP900_RIP"}:
                    # These new models have no exact Gutenprint backend. Sizes
                    # absent from their own TPP use its confirmed Custom byte,
                    # rather than inventing an unverified model-local selector.
                    size_code = 99
                width_pt, height_pt = width_mm * 72 / 25.4, height_mm * 72 / 25.4
                result.append(PageCapability(
                    PpdPage(name, label, width_pt, height_pt, 9.0, 9.0,
                            width_pt - 9.0, height_pt - 9.0),
                    True, True, media_size_code=size_code,
                    physical_media_size_mm=(width_mm, height_mm),
                    native_page_format_mils=(round(width_mm * 1000 / 25.4),
                                             round(height_mm * 1000 / 25.4), 120),
                ))
        if r2880 or r3000:
            result.append(PageCapability(
                PpdPage("Roll13x19", "13 × 19 in (roll)",
                        13 * 72.0, 19 * 72.0, 9.0, 9.0,
                        13 * 72.0 - 9.0, 19 * 72.0 - 9.0),
                True, True, media_size_code=61, sheet_geometry_class="roll",
                native_page_format_mils=(13000, 19000, 120),
            ))
        if (source.get("driver") or {}).get("id") == "ET8550":
            # Epson specifies rear-slot panoramic sheets up to 2 m for this
            # family. ET-8500/L8160 take A4 width; ET-8550/L8180 also take A3+.
            widths = (210, 329) if any(p.media_width_mm > 300 for p in result) else (210,)
            for width_mm in widths:
                width_pt, height_pt = width_mm * 72 / 25.4, 2000 * 72 / 25.4
                result.append(PageCapability(
                    PpdPage(f"LongSheet{width_mm}x2000", f"{width_mm} × 2000 mm",
                            width_pt, height_pt, 3 * 72 / 25.4, 20 * 72 / 25.4,
                            width_pt - 3 * 72 / 25.4, height_pt - 3 * 72 / 25.4),
                    True, True, media_size_code=99, sheet_geometry_class="long_sheet",
                    physical_media_size_mm=(float(width_mm), 2000.0),
                ))
        return tuple(result)
    result: list[PageCapability] = []
    for index, item in enumerate(raw_sizes):
        width = int(item.get("width_mils") or 0) * 72 / 1000
        height = int(item.get("height_mils") or 0) * 72 / 1000
        if width <= 0 or height <= 0 or str(item.get("name", "")).casefold() == "custom":
            continue
        name = _identifier(str(item.get("name") or ""), f"Page{index}")
        borderless = "borderless" in str(item.get("name") or "").casefold()
        margin = 0.0 if borderless else min(9.0, width / 20, height / 20)
        media_size_code, geometry_class, special_selector, native_format, physical_size = page_fields(item)
        result.append(
            PageCapability(
                PpdPage(
                    name,
                    str(item.get("name") or name),
                    width,
                    height,
                    margin,
                    margin,
                    width - margin,
                    height - margin,
                ),
                True,
                True,
                media_size_code=media_size_code,
                sheet_geometry_class=geometry_class,
                special_media_selector=special_selector,
                native_page_format_mils=native_format,
                physical_media_size_mm=physical_size,
            )
        )
    return tuple(result)


def _feeds(
    source: dict[str, Any], runtime: dict[str, Any], *,
    prefer_model_feeds: bool = False,
    include_product_roll_feed: bool = False,
) -> tuple[InputSourceCapability, ...]:
    result: list[InputSourceCapability] = []
    # A compatible Gutenprint model supplies raster mechanics, but may inherit
    # input slots from another printer (model 91 uses the R1800 roll slots for
    # XP-830). Audited product descriptors have decoded PP paths, so use their
    # model-specific feeds. Other packages retain their existing inherited
    # initialization/cutting sequences until their feed routing is checked.
    product_feeds = source.get("feeds") or ()
    has_product_mechanics = bool(product_feeds) and all(
        (item.get("feed_mechanics") or {}).get("remote_paper_path")
        for item in product_feeds
    )
    if prefer_model_feeds and has_product_mechanics:
        records = product_feeds
    elif runtime.get("schema") == "pytone-rip.gutenprint-runtime":
        records = runtime.get("feeds") or ()
        if include_product_roll_feed:
            # P800's compatible Gutenprint feed list omits its optional roll
            # unit. Keep the existing sheet setup commands and add only the
            # model-specific roll path decoded from ReferenceDriver's Paper.Param.
            roll_feeds = [
                item for item in product_feeds
                if (item.get("feed_mechanics") or {}).get("medium_kind") == "roll"
            ]
            if len(roll_feeds) != 1 or not (
                roll_feeds[0].get("feed_mechanics") or {}
            ).get("remote_paper_path"):
                raise ValueError("P800 roll feed is missing its paper path")
            if not any(
                (item.get("feed_mechanics") or {}).get("medium_kind") == "roll"
                for item in records
            ):
                records = [*records, roll_feeds[0]]
    else:
        records = product_feeds or runtime.get("feeds") or ()
    for index, item in enumerate(records):
        mechanics = item.get("feed_mechanics") or {}
        path = mechanics.get("remote_paper_path") or {}
        name = str(item.get("text") or item.get("name") or f"Feed {index + 1}")
        medium_kind = str(
            mechanics.get("medium_kind")
            or ("roll" if "roll" in name.casefold() else "automatic_cut_sheet")
        )
        result.append(
            InputSourceCapability(
                identity=_identifier(str(item.get("name") or ""), f"Feed{index}"),
                display_name=name,
                source="normalized Epson feed mechanics",
                implemented=True,
                path_type=int(path.get("type", 1)),
                path_number=int(path.get("number", 1)),
                medium_kind=medium_kind,
                roll_cut_mode=str(mechanics.get("roll_cut_mode") or "none"),
                cutter_edge_trim_mode=(
                    str(mechanics["cutter_edge_trim_mode"])
                    if mechanics.get("cutter_edge_trim_mode") is not None
                    else None
                ),
                suppress_form_feed_after_page=bool(
                    mechanics.get("suppress_form_feed_after_page", False)
                ),
                initialization_commands=tuple(
                    (str(record["command"]), bytes.fromhex(record["payload_hex"]))
                    for record in mechanics.get("initialization_commands", ())
                ),
                deinitialization_commands=tuple(
                    (str(record["command"]), bytes.fromhex(record["payload_hex"]))
                    for record in mechanics.get("deinitialization_commands", ())
                ),
            )
        )
    if not result:
        result.append(InputSourceCapability("Standard", "Standard feed", "import fallback", True, path_type=1, path_number=1))
    # Keep distinct physical feed commands. A protocol value such as PP 01 FF
    # is not by itself an Auto choice: it is also the proven Sheet command on
    # several Pro models. Filter only descriptors that actually offer an Auto
    # selection alongside explicit physical feeds.
    unique: list[InputSourceCapability] = []
    seen: set[tuple[object, ...]] = set()
    for feed in result:
        mechanics = (
            feed.path_type, feed.path_number, feed.medium_kind,
            feed.roll_cut_mode, feed.cutter_edge_trim_mode,
            feed.suppress_form_feed_after_page,
            feed.initialization_commands, feed.deinitialization_commands,
        )
        if mechanics not in seen:
            unique.append(feed)
            seen.add(mechanics)
    if len(unique) == 2:
        by_name = {feed.identity: feed for feed in unique}
        automatic = by_name.get("Automatic_Feed")
        manual = by_name.get("Manual_Feed")
        if (
            automatic is not None and manual is not None
            and (automatic.path_type, automatic.path_number) == (1, 0)
            and (manual.path_type, manual.path_number) == (0, 0)
            and (
                automatic.medium_kind, automatic.roll_cut_mode,
                automatic.cutter_edge_trim_mode,
                automatic.suppress_form_feed_after_page,
                automatic.initialization_commands,
                automatic.deinitialization_commands,
            ) == (
                manual.medium_kind, manual.roll_cut_mode,
                manual.cutter_edge_trim_mode,
                manual.suppress_form_feed_after_page,
                manual.initialization_commands,
                manual.deinitialization_commands,
            )
        ):
            # On these one-tray models ReferenceDriver calls the normal sheet path
            # "Automatic Feed" and the other code "Manual Feed". The normal
            # path selects that sole physical tray, not between multiple trays.
            unique = [automatic]
    if len(unique) > 1:
        unique = [
            feed for feed in unique
            if not feed.identity.casefold().startswith("auto")
            and not feed.display_name.casefold().startswith("auto")
        ]
        if not unique:
            raise ValueError("Imported printer has no explicit physical feed")
    if len(unique) == 1:
        unique[0] = replace(unique[0], identity="Standard", display_name="Standard feed")
    return tuple(unique)


def _media(
    source: dict[str, Any],
    modes: tuple[PrinterMode, ...],
    package: CatalogPackage,
    runtime: dict[str, Any],
    ppd: PpdCapabilities,
    reference_driver_papers: dict[str, int] | None = None,
) -> tuple[tuple[MediaCapability, ...], tuple[tuple[str, str], ...]]:
    # Keep the union of Gutenprint and ReferenceDriver MI codes for these models.
    # Where both sources use one code with different names, the product's
    # ReferenceDriver paper name wins. P800 code 11 is Plain Paper in a captured
    # ReferenceDriver stream, while Gutenprint calls that code Premium Glossy.
    merge_product_media = package.package_id in {
        "pf--Epson_SureColorP400",
        "pf--Epson_SureColorP600",
        "pf--Epson_SureColorP800",
    }
    use_technical_media = runtime.get("schema") == "pytone-rip.gutenprint-runtime"
    records = (
        runtime.get("paper_types") or source.get("paper_types")
        if use_technical_media
        else source.get("media")
    )
    result: list[MediaCapability] = []
    for index, item in enumerate(records or ()):
        params = item.get("params") or {}
        if use_technical_media:
            media_codes = _integers(params.get("PaperMedia"))
            methods = tuple(int(value, 0) for value in params.get("PrintMethod", ()))
            # Gutenprint sends REMOTE1 MI only when PaperMedia is active.
            # An absent parameter differs from an explicit PaperMedia=0.
            media_code = media_codes[0] if media_codes else (
                (reference_driver_papers or {}).get(
                    _GUTENPRINT_TO_REFERENCE_DRIVER_PAPER.get(str(item.get("name")), "")
                )
            )
            legacy_media_type_code = None
            special_media_selector = None
            physical_raster_layout_variant = 0
        else:
            mechanics = item.get("media_mechanics") or {}
            special = mechanics.get("special_media") or {}
            # T7000 Paper.Param values select native media mechanics, but its
            # captured ReferenceDriver streams omit REMOTE1 MI for every paper tested.
            media_code = (
                None if package.package_id == "pf--Epson_SureColorT7000"
                else int(mechanics.get("remote_media_type_code", 0))
            )
            legacy_media_type_code = int(
                mechanics.get("legacy_media_type_code", 0)
            )
            special_media_selector = (
                int(special["selector"]) if "selector" in special else None
            )
            physical_raster_layout_variant = int(
                mechanics.get("physical_raster_layout_variant", 0)
            )
            methods = ()

        name = str(item.get("text") or item.get("name") or f"Media {index + 1}")
        source_index = int(item.get("idx", index))
        category = _media_class(name + " " + str(item.get("match") or ""))
        has_specific_vendor_modes = any(
            mode.media_class == f"pf:{source_index}" for mode in modes
        )
        supported = tuple(
            mode.identity
            for mode in modes
            if mode.media_class in {"generic", category, f"pf:{source_index}"}
            or (mode.media_class == "pf:0" and source_index == 0)
            or (
                mode.media_class == "pf:X"
                and source_index != 0
                and not has_specific_vendor_modes
            )
            or {mode.media_class, category} <= {"matte", "plain"}
        ) or tuple(mode.identity for mode in modes)
        if methods:
            method_pairs = tuple(
                (mode.identity, methods[min(mode_index, len(methods) - 1)])
                for mode_index, mode in enumerate(modes)
            )
        elif use_technical_media:
            method_pairs = ()
        else:
            method_pairs = tuple(
                (
                    mode.identity,
                    mode.escp2_print_method_override
                    if mode.escp2_print_method_override is not None
                    else 0,
                )
                for mode in modes
            )
        result.append(
            MediaCapability(
                identity=_identifier(name, f"Media{index}"),
                display_name=name,
                source="normalized Epson media mechanics",
                implemented=True,
                gutenprint_name=str(item.get("name") or name),
                media_code=media_code,
                legacy_media_type_code=legacy_media_type_code,
                special_media_selector=special_media_selector,
                physical_raster_layout_variant=physical_raster_layout_variant,
                print_methods=method_pairs,
                supported_modes=supported,
                media_class=category,
                paper_thickness_code=_parameter_byte(
                    params, "PaperThickness", context=package.package_id
                ),
                vacuum_intensity_code=_parameter_byte(
                    params, "VacuumIntensity", context=package.package_id
                ),
                feed_adjustment_code=_parameter_byte(
                    params, "FeedAdjustment", context=package.package_id
                ),
                feed_sequence_code=_parameter_byte(
                    params, "FeedSequence", context=package.package_id
                ),
                platen_gap_code=_parameter_byte(
                    params, "PlatenGap", context=package.package_id
                ),
            )
        )
    if use_technical_media and package.package_id.startswith("pf--"):
        # A matched Gutenprint runtime enriches raster mechanics and advanced
        # controls, but it is not allowed to erase native media selectors from
        # the product-specific ReferenceDriver descriptor. Add only selector tuples
        # absent from the technical table; command-identical names are still
        # collapsed below by _canonical_media.
        existing_commands = {
            (
                medium.media_code,
                medium.legacy_media_type_code or 0,
                medium.special_media_selector or 0,
                medium.physical_raster_layout_variant,
            )
            for medium in result
        }
        for index, item in enumerate(source.get("media") or ()):
            mechanics = item.get("media_mechanics") or {}
            special = mechanics.get("special_media") or {}
            media_code = int(mechanics.get("remote_media_type_code", 0))
            legacy_media_type_code = int(
                mechanics.get("legacy_media_type_code", 0)
            )
            special_media_selector = (
                int(special["selector"]) if "selector" in special else None
            )
            physical_raster_layout_variant = int(
                mechanics.get("physical_raster_layout_variant", 0)
            )
            command = (
                media_code,
                legacy_media_type_code,
                special_media_selector or 0,
                physical_raster_layout_variant,
            )
            if command in existing_commands and not merge_product_media:
                continue
            name = str(item.get("name") or f"Media {index + 1}")
            category = _media_class(name + " " + str(item.get("match") or ""))
            supported = tuple(
                mode.identity
                for mode in modes
                if mode.media_class in {"generic", category}
                or {mode.media_class, category} <= {"matte", "plain"}
            ) or tuple(mode.identity for mode in modes)
            result.append(
                MediaCapability(
                    identity=_identifier(name, f"Media{index}"),
                    display_name=name,
                    source="normalized Epson product media mechanics",
                    implemented=True,
                    gutenprint_name=name,
                    media_code=media_code,
                    legacy_media_type_code=legacy_media_type_code,
                    special_media_selector=special_media_selector,
                    physical_raster_layout_variant=physical_raster_layout_variant,
                    print_methods=tuple(
                        (
                            mode.identity,
                            mode.escp2_print_method_override
                            if mode.escp2_print_method_override is not None
                            else 0,
                        )
                        for mode in modes
                    ),
                    supported_modes=supported,
                    media_class=category,
                )
            )
            existing_commands.add(command)
    if package.package_id in {
        "pf--Epson_SureColorP700_RIP", "pf--Epson_SureColorP900_RIP",
    }:
        by_name = {medium.display_name: medium for medium in result}
        existing_codes = {medium.media_code for medium in result}
        for name, code, template_name, category in _P700_P900_ADDITIONAL_MEDIA:
            if code in existing_codes:
                continue
            template = by_name[template_name]
            # RIP code 11 already used the unsuffixed Photo Glossy ID as an
            # alias for Premium Glossy. Keep saved selections on that code.
            new_id = _identifier(name, "Media")
            if name in by_name:
                new_id = f"{new_id}_MI{code:02X}"
            result.append(replace(
                template,
                identity=new_id,
                display_name=name,
                source="ReferenceDriver P700/P900 RGB TPP and captured REMOTE1 MI",
                gutenprint_name=name,
                media_code=code,
                media_class=category,
            ))
            existing_codes.add(code)
    if not result:
        result.append(
            MediaCapability(
                "Plain",
                "Plain paper",
                "import fallback",
                True,
                media_code=0,
                print_methods=tuple((mode.identity, 0) for mode in modes),
                supported_modes=tuple(mode.identity for mode in modes),
                media_class="plain",
            )
        )
    driver_id = package.package_id.removeprefix("gp--")
    captured_media = CAPTURED_PRO_MEDIA_BY_DRIVER.get(driver_id)
    synthetic_ppd = any(
        marker in ppd.metadata.get("NickName", "").casefold()
        for marker in ("pytone", "pyrip")
    )
    if use_technical_media and captured_media and synthetic_ppd:
        # ReferenceDriver captures override conflicting Gutenprint names/codes, but
        # the exact Gutenprint model also defines additional PaperMedia values.
        # Keep those distinct selectors; the synthetic PPD is not evidence
        # that they are absent. Uncaptured selectors still need live checking.
        by_gutenprint_name = {item.gutenprint_name: item for item in result}
        captured_rows = [
            replace(
                by_gutenprint_name[item.gutenprint_name],
                identity=_identifier(item.display_name, "Media"),
                display_name=item.display_name,
                source="ReferenceDriver .tpp and captured REMOTE1 MI",
                media_code=item.media_code,
            )
            for item in captured_media
        ]
        captured_codes = {item.media_code for item in captured_rows}
        result = captured_rows + [
            item for item in result if item.media_code not in captured_codes
        ]
    ppd_aliases: tuple[tuple[str, str], ...] = ()
    ppd_nickname = ppd.metadata.get("NickName", "").casefold()
    ppd_manufacturer = ppd.metadata.get("Manufacturer", "").casefold()
    if "epson" in ppd_manufacturer and not any(
        marker in ppd_nickname for marker in ("pytone", "pyrip")
    ):
        # Some ESC/P-R PPDs expose only Plain and Premium Glossy, whereas
        # their exact Gutenprint ESC/P2 models contain additional PaperMedia
        # selectors. Keep the PPD names/IDs and publish the remaining native
        # choices so the monochrome GUI is not limited by the RGB driver UI.
        include_native = use_technical_media and package.package_id.startswith("gp--")
        normalized, ppd_aliases = _ppd_named_media(
            tuple(result), ppd, include_unmatched_native=include_native
        )
        if package.protocol_family == "ET8550":
            # The RGB PPD omits film. Exact ET-8500/8550 captures confirm
            # its distinct MI 01 and dedicated paper-8 quality modes.
            # Do not add branded duplicates, CD or DTF-only media.
            normalized += tuple(medium for medium in result if medium.media_code == 1)
    else:
        # A synthetic PPD or one for another manufacturer is not Epson evidence.
        normalized = tuple(result)
    if package.package_id == "gp--escp2-r2880":
        # Neither the Epson R2880 media list nor its installed ReferenceDriver TPP
        # offers ColorLife. Gutenprint's generic Other has no PaperMedia code.
        # Keep distinct native Gutenprint codes, and add the two ReferenceDriver-only
        # MI selectors confirmed in live PRNs without reusing their code/name.
        normalized = tuple(
            medium for medium in normalized
            if medium.gutenprint_name not in {"ColorLife", "Other"}
        )
        by_name = {medium.display_name: medium for medium in normalized}
        normalized += (
            replace(
                by_name["Inkjet Paper"], identity="Epson_Inkjet_Paper",
                display_name="Epson Inkjet Paper", media_code=29,
                source="ReferenceDriver R2880 REMOTE1 MI capture",
            ),
            replace(
                by_name["Photo Paper"], identity="Epson_Photo_Glossy",
                display_name="Epson Photo Glossy", media_code=14,
                source="ReferenceDriver R2880 REMOTE1 MI capture",
            ),
        )
    if package.package_id == "gp--escp2-r2000":
        # A fresh product capture sends MI 0e for Epson Photo Glossy. The
        # exact Gutenprint table's Photo choice uses a different code, 43.
        template = next(item for item in normalized if item.gutenprint_name == "Photo")
        normalized += (replace(template, identity="Epson_Photo_Glossy",
            display_name="Epson Photo Glossy", media_code=14,
            source="ReferenceDriver R2000 captured REMOTE1 MI"),)
    if package.package_id == "gp--escp2-r3000":
        # The product's paper list omits ColorLife and the generic Other has
        # no MI byte. Keep one public selector per code for this model.
        normalized = tuple(
            medium for medium in normalized
            if medium.gutenprint_name not in {"ColorLife", "Other"}
        )
        by_name = {medium.display_name: medium for medium in normalized}
        normalized += (
            replace(
                by_name["Inkjet Paper"], identity="Epson_Inkjet_Paper",
                display_name="Epson Inkjet Paper", media_code=29,
                source="R3000 product paper descriptor",
            ),
            replace(
                by_name["Photo Paper"], identity="Epson_Photo_Glossy",
                display_name="Epson Photo Glossy", media_code=14,
                source="R3000 product paper descriptor",
            ),
        )
        normalized = tuple(
            replace(medium, paper_density=_R3000_PAPER_DENSITY[medium.gutenprint_name])
            for medium in normalized
        )
    if package.model_id.startswith(("Stylus_Pro_", "SureColor_P", "SC_P")) or package.package_id in {
        "pf--Epson_SureColorP700_RIP", "pf--Epson_SureColorP900_RIP",
    }:
        consumer_only = ("postcard", "envelope", "cd", "dvd", "colorlife")
        normalized = tuple(
            medium
            for medium in normalized
            if not any(
                token in f"{medium.identity} {medium.display_name}".casefold()
                for token in consumer_only
            )
        )
    if merge_product_media:
        # The technical table's "Other" has no MI value. It cannot represent
        # a unique native media type and is absent from the product descriptor.
        normalized = tuple(item for item in normalized if item.media_code is not None)
    # A generic Other entry is not an actionable media choice. Do not publish
    # it even when an imported technical table assigns a placeholder MI byte.
    normalized = tuple(item for item in normalized if not any(
        str(value or "").strip().casefold() == "other"
        for value in (item.identity, item.display_name, item.gutenprint_name)
    ))
    canonical, native_aliases = _canonical_media(
        normalized,
        modes,
        unique_media_codes=True,
        prefer_technical_media=use_technical_media and not merge_product_media,
        prefer_product_media=merge_product_media or bool(captured_media and synthetic_ppd),
        preferred_product_media_by_code=(
            {11: "Epson Premium Glossy"}
            if package.package_id == "pf--Epson_SureColorP800" else None
        ),
    )
    if package.package_id == "pf--Epson_SureColorP800":
        canonical = tuple(replace(
            medium, paper_density=_P800_PAPER_DENSITY_BY_MI[medium.media_code]
        ) for medium in canonical)
        # The synthetic PPD uses the older glossy ID. Keep saved selections,
        # and map the old duplicate Plain MI0B choice to the distinct plain MI0.
        native_aliases += (
            ("Premium_Glossy_Photo_Paper", "Epson_Premium_Glossy"),
            ("Plain_paper_MI0B", "Plain_Paper"),
        )
    canonical_ids = {medium.identity.casefold() for medium in canonical}
    aliases = tuple((alias, target)
        for alias, target in _flatten_aliases((*ppd_aliases, *native_aliases))
        if alias.casefold() != "other" and target.casefold() in canonical_ids)
    return canonical, aliases


def _drying_choices(
    *, selector: int, page_units: bool, maximum_scan_seconds: int = 10
) -> tuple[DeviceSettingChoice, ...]:
    """Build exact REMOTE1 DR choices for an advertised drying feature."""

    seconds = (
        (0, 1, 2, 3, 5, 10, 15, 30, 60)
        if page_units
        else tuple(
            value
            for value in (0, 0.1, 0.2, 0.3, 0.5, 1, 2, 3, 5, 10)
            if value <= maximum_scan_seconds
        )
    )
    result: list[DeviceSettingChoice] = []
    for value in seconds:
        encoded_value = int(value if page_units else round(value * 1000))
        label = "Off" if value == 0 else f"{value:g} s"
        identity = "Off" if value == 0 else _identifier(f"{value:g}sec", "Dry")
        result.append(
            DeviceSettingChoice(
                identity,
                label,
                "DR",
                bytes((0, selector)) + encoded_value.to_bytes(2, "little"),
            )
        )
    return tuple(result)


def _fixed_setting(
    identity: str,
    display_name: str,
    command: str,
    values: tuple[tuple[str, str, bytes], ...],
) -> DeviceSettingCapability:
    """Create one shared, fully named model capability definition."""

    choices = tuple(
        DeviceSettingChoice(choice_id, label, command, payload)
        for choice_id, label, payload in values
    )
    return DeviceSettingCapability(identity, display_name, choices[0].identity, choices)


def _runtime_parameter_values(runtime: dict[str, Any], name: str) -> tuple[int, ...]:
    """Return the finite model-specific values actually used by its media.

    The full protocol field is one byte, but presenting 256 unexplained values
    would invite unsafe guesses. The GUI therefore offers Auto plus only codes
    found in this model's open media table. / Показываем только значения,
    реально объявленные для бумаг этой модели, а не выдуманный диапазон 0..255.
    """

    values: set[int] = set()
    for medium in runtime.get("paper_types") or ():
        parsed = _integers((medium.get("params") or {}).get(name))
        if parsed:
            value = parsed[0]
            if not 0 <= value <= 0xFF:
                raise ValueError(f"{name} must fit one protocol byte")
            values.add(value)
    return tuple(sorted(values))


def _automatic_runtime_setting(
    source_name: str, values: tuple[int, ...]
) -> DeviceSettingCapability:
    """Build a named Auto/override control for one media-mechanics field."""

    definitions = {
        "PaperThickness": (
            "Paper_Thickness",
            "Paper thickness",
            "PH",
            lambda value: bytes((0, value)),
            lambda value: f"Thickness code {value}",
        ),
        "VacuumIntensity": (
            "Paper_Suction",
            "Paper suction / vacuum",
            "SN",
            lambda value: bytes((0, 5, value)),
            lambda value: f"Vacuum code {value}",
        ),
        "FeedAdjustment": (
            "Feed_Adjustment",
            "Paper feed adjustment",
            "SN",
            lambda value: bytes((0, 4, value)),
            lambda value: f"{value / 100:.2f}% (code {value})",
        ),
        "FeedSequence": (
            "Feed_Sequence",
            "Paper feed sequence",
            "SN",
            lambda value: bytes((0, 0, value)),
            lambda value: f"Sequence {value}",
        ),
        "PlatenGap": (
            "Platen_Gap",
            "Platen gap",
            "US",
            lambda value: bytes((0, 1, value)),
            lambda value: f"Gap code {value}",
        ),
    }
    identity, display_name, command, payload, label = definitions[source_name]
    choices = [DeviceSettingChoice("Auto", "Media default (Auto)", "", b"")]
    choices.extend(
        DeviceSettingChoice(
            f"Code_{value}", label(value), command, payload(value)
        )
        for value in values
    )
    return DeviceSettingCapability(identity, display_name, "Auto", tuple(choices))


def _feature_setting(identity: str) -> DeviceSettingCapability:
    """Translate an open capability name to one documented Epson command.

    Model packages contain only these names; repeated positional value strings
    are deliberately absent. Command bytes are defined once here and covered
    by byte-level tests. / Package модели объявляет возможность, а общая
    реализация команды и её вариантов находится в одном месте.
    """

    if identity == "scan_drying_time":
        choices = _drying_choices(
            selector=0, page_units=False, maximum_scan_seconds=5
        )
        return DeviceSettingCapability(
            "Drying_Time", "Drying time after print-head pass", "Off", choices
        )
    if identity == "page_drying_time":
        choices = _drying_choices(selector=1, page_units=True)
        return DeviceSettingCapability(
            "Page_Drying_Time", "Drying time after page", "Off", choices
        )
    if identity == "minimum_scan_drying_time":
        choices = _drying_choices(selector=0x40, page_units=False)
        return DeviceSettingCapability(
            "Minimum_Scan_Drying_Time",
            "Minimum drying time after print-head pass",
            "Off",
            choices,
        )
    if identity == "paper_thickness":
        return _fixed_setting(
            "Paper_Thickness",
            "Paper thickness",
            "PH",
            tuple(
                (
                    "Default" if value == 0 else _identifier(f"{value / 10:.1f}mm", "Thickness"),
                    "Default" if value == 0 else f"{value / 10:.1f} mm",
                    # PH is ``00 <value>``. The leading zero is part of the
                    # Epson command payload, not a generic length byte.
                    bytes((0, value)),
                )
                for value in range(16)
            ),
        )
    if identity == "paper_suction":
        return _fixed_setting(
            "Paper_Suction",
            "Paper suction",
            "SN",
            (
                ("Default", "Default", bytes((0, 5, 51))),
                ("Minus_1", "-1", bytes((0, 5, 26))),
                ("Minus_2", "-2", bytes((0, 5, 16))),
                ("Minus_3", "-3", bytes((0, 5, 6))),
                ("Minus_4", "-4 (thin paper)", bytes((0, 5, 4))),
            ),
        )
    if identity == "save_roll_paper":
        return _fixed_setting(
            "Save_Paper_Roll_Paper",
            "Save roll paper",
            "LB",
            (
                ("Off", "Off", bytes((0, 1, 0))),
                ("On", "On", bytes((0, 1, 1))),
            ),
        )
    if identity == "platen_gap":
        return _fixed_setting(
            "Platen_Gap",
            "Platen gap",
            "US",
            (
                ("Default", "Default", bytes((0, 1, 0))),
                ("Large", "Large", bytes((0, 1, 1))),
            ),
        )
    if identity == "microweave_precision":
        # This is a mode-local selector, not a REMOTE1 device command. Model A
        # uses print_method.default; Model B uses print_method.precision_variant
        # when that byte exists in the selected mode.
        #
        # Настройка выбирает один из двух ESC (m режима и не отправляет
        # отдельную remote-команду.
        return DeviceSettingCapability(
            "Microweave_Precision",
            "Microweave precision correction",
            "Model_A",
            (
                DeviceSettingChoice("Model_A", "Model A", "", b""),
                DeviceSettingChoice(
                    "Model_B", "Model B", "", b"", use_alternate_print_method=True
                ),
            ),
        )
    raise ValueError(f"Unknown implemented device feature: {identity}")


def _settings(
    source: dict[str, Any], runtime: dict[str, Any]
) -> tuple[DeviceSettingCapability, ...]:
    feature_names = [str(item) for item in source.get("device_features") or ()]
    runtime_names = {
        "PageDryTime": "page_drying_time",
        "ScanDryTime": "scan_drying_time",
        "ScanMinDryTime": "minimum_scan_drying_time",
        "PlatenGap": "platen_gap",
    }
    feature_names.extend(
        runtime_names[item]
        for item in runtime.get("device_features") or ()
        if item in runtime_names
    )
    unique = tuple(dict.fromkeys(feature_names))
    result = [_feature_setting(identity) for identity in unique]
    existing = {item.identity for item in result}
    for source_name in (
        "PaperThickness",
        "VacuumIntensity",
        "FeedAdjustment",
        "FeedSequence",
        "PlatenGap",
    ):
        if source_name not in (runtime.get("device_features") or ()):
            continue
        values = _runtime_parameter_values(runtime, source_name)
        if not values:
            continue
        setting = _automatic_runtime_setting(source_name, values)
        if setting.identity not in existing:
            result.append(setting)
            existing.add(setting.identity)
    return tuple(result)


def _with_feature_settings(
    settings: tuple[DeviceSettingCapability, ...],
    feature_names: tuple[str, ...],
) -> tuple[DeviceSettingCapability, ...]:
    """Add confirmed model controls without duplicating imported settings."""

    result = list(settings)
    existing = {item.identity for item in result}
    for feature_name in feature_names:
        setting = _feature_setting(feature_name)
        if setting.identity not in existing:
            result.append(setting)
            existing.add(setting.identity)
    return tuple(result)


def _normalize_photo_model_controls(
    package: CatalogPackage,
    media: tuple[MediaCapability, ...],
    settings: tuple[DeviceSettingCapability, ...],
) -> tuple[tuple[MediaCapability, ...], tuple[DeviceSettingCapability, ...]]:
    """Apply hardware-specific controls omitted or overstated by imports."""

    if package.package_id in {"gp--escp2-r2400", "gp--escp2-r2880", "gp--escp2-r3000"}:
        # Epson's paper-configuration UI documents per-pass drying for these
        # models. R3000's product descriptor additionally supplies PH 0..15.
        settings = _with_feature_settings(settings, ("scan_drying_time",))
        if package.package_id == "gp--escp2-r3000":
            settings = _with_feature_settings(settings, ("paper_thickness",))

    if package.package_id in {"gp--escp2-3800", "gp--escp2-3880"}:
        # The desktop A2 3800/3880 models have no paper-vacuum system.
        # Gutenprint exposes a legacy VacuumIntensity field for these models,
        # but emitting it would advertise and send a nonexistent control.
        media = tuple(
            replace(medium, vacuum_intensity_code=None) for medium in media
        )
        settings = tuple(
            setting for setting in settings if setting.identity != "Paper_Suction"
        )
        # Epson 3880's UI resource IDs 6554..6558 label the stored values
        # Auto/Wider/Wide/Standard/Narrow. The UI writes that value directly
        # to the advanced-paper word at +0x1a; the output module sends it
        # unchanged as US 00 01 <value>. Keep the old Default/Large IDs and
        # their bytes so existing configurations do not silently change gap.
        gap = _fixed_setting("Platen_Gap", "Platen gap", "US", (
            ("Default", "Media default", bytes((0, 1, 0))),
            ("Large", "Wider", bytes((0, 1, 1))),
            ("Wide", "Wide", bytes((0, 1, 2))),
            ("Standard", "Standard", bytes((0, 1, 3))),
            ("Narrow", "Narrow", bytes((0, 1, 4))),
        ))
        settings = tuple(gap if setting.identity == "Platen_Gap" else setting
                         for setting in settings)

    if package.package_id in {
        "pf--Epson_SureColorP600",
        "pf--Epson_SureColorP800",
    }:
        # These desktop SureColor models provide Epson advanced-media controls
        # for drying and thickness, but not paper suction/vacuum.
        media = tuple(
            replace(medium, vacuum_intensity_code=None) for medium in media
        )
        settings = tuple(
            setting for setting in settings if setting.identity != "Paper_Suction"
        )
        settings = _with_feature_settings(
            settings,
            (
                "scan_drying_time",
                "page_drying_time",
                "minimum_scan_drying_time",
                "paper_thickness",
                "platen_gap",
            ),
        )

    if package.package_id in {
        "pf--Epson_SureColorP700_RIP",
        "pf--Epson_SureColorP900_RIP",
    }:
        settings = _with_feature_settings(
            settings,
            (
                "scan_drying_time",
                "page_drying_time",
                "paper_thickness",
                "platen_gap",
            ),
        )

    return media, settings


def load_catalog_model(
    package: CatalogPackage, root: Path = CATALOG_ROOT
) -> PrinterModel:
    directory = root / "packages" / package.package_id
    source = json.loads((directory / "printer.json").read_text(encoding="utf-8"))
    runtime_path = directory / (package.runtime_source_file or "runtime-source.json")
    runtime = json.loads(runtime_path.read_text(encoding="utf-8")) if runtime_path.is_file() else {}
    backend_source_path = directory / (
        package.backend_source_file or "backend-source.json"
    )
    backend_source = (
        json.loads(backend_source_path.read_text(encoding="utf-8"))
        if backend_source_path.is_file()
        else source
    )
    backend_runtime_path = directory / (
        package.backend_runtime_file or "backend-runtime.json"
    )
    backend_runtime = (
        json.loads(backend_runtime_path.read_text(encoding="utf-8"))
        if backend_runtime_path.is_file()
        else runtime
    )
    reference_driver_peer = _REFERENCE_DRIVER_PAPER_PEERS.get(package.package_id)
    reference_driver_source_path = (
        root / "packages" / reference_driver_peer / "printer.json"
        if reference_driver_peer is not None
        else None
    )
    reference_driver_source = (
        json.loads(reference_driver_source_path.read_text(encoding="utf-8"))
        if reference_driver_source_path is not None and reference_driver_source_path.is_file()
        else {}
    )
    reference_driver_papers = {
        str(item.get("name")): int(
            (item.get("media_mechanics") or {}).get("remote_media_type_code", 0)
        )
        for item in reference_driver_source.get("media", ())
    }
    ppd = read_ppd(directory / package.ppd_file)
    if package.package_id.startswith("gp--"):
        modes = _gutenprint_modes(source, backend_runtime)
        rows_per_pass = max((mode.nozzle_count for mode in modes), default=1)
        profile = gutenprint_profile(
            package.protocol_family,
            rows_per_pass=rows_per_pass,
            source="Gutenprint ESC/P2 model data",
        )
    elif package.protocol_family.startswith("gutenprint-model-"):
        technical_modes = _gutenprint_modes(backend_source, backend_runtime)
        rows_per_pass = max((mode.nozzle_count for mode in technical_modes), default=1)
        profile = gutenprint_profile(
            package.protocol_family,
            rows_per_pass=rows_per_pass,
            source="Gutenprint model XML",
        )
        # A matched model XML is the authoritative source for raster mechanics:
        # it contains the complete resolution range, bit depth, drop sizes and
        # head geometry. The product descriptor still supplies identity and any
        # model-specific settings. Mixing its shortened UI mode list with the
        # technical backend data previously hid P600/P800 high-resolution modes.
        modes = technical_modes
    else:
        profile = ESCP2_FAMILY_PROFILES[package.protocol_family]
        modes = _vendor_modes(
            source, runtime, profile.rows_per_pass,
            protocol_family=package.protocol_family,
        )
    if not modes:
        raise ValueError(f"Imported printer has no resolution modes: {package.package_id}")
    page_source = source
    if not source.get("media_sizes") and reference_driver_source.get("media_sizes"):
        page_source = {**source, "media_sizes": reference_driver_source["media_sizes"]}
    pages = _pages(
        page_source,
        ppd,
        backend_runtime,
        match_size_by_dimensions=(page_source is not source
                                  or package.package_id == "pf--Epson_EcoTank_ET2550"),
        allow_synthetic_wide_pages=package.ppd_source != "vendor.ppd",
        trust_ppd_imageable_area=package.ppd_source == "vendor.ppd",
        include_unmatched_native_pages=package.package_id == "pf--Epson_EcoTank_ET2550",
    )
    if (
        package.ppd_source != "vendor.ppd"
        and package.package_id.removeprefix("gp--") in CAPTURED_PRO_MEDIA_BY_DRIVER
    ):
        # Every Size.Param in these ReferenceDriver descriptors is zero, and captured
        # MI packets carry 00 in the media-size position for each tested paper.
        pages = tuple(replace(page, media_size_code=0) for page in pages)
    media, media_aliases = _media(
        source, modes, package, backend_runtime, ppd, reference_driver_papers
    )
    device_settings = _settings(source, backend_runtime)
    media, device_settings = _normalize_photo_model_controls(
        package, media, device_settings
    )
    # A descriptor can contain modes dedicated only to a branded ICC medium
    # removed by _canonical_media. Such a mode has no valid selectable medium
    # in this monochrome RIP and must not survive as an orphan GUI choice.
    # Если режим относился только к удалённой цветовой/брендированной бумаге,
    # его тоже не показываем: физически допустимой пары носитель+режим нет.
    supported_mode_ids = {
        mode_id for medium in media for mode_id in medium.supported_modes
    }
    modes = tuple(mode for mode in modes if mode.identity in supported_mode_ids)
    if package.package_id in _REFERENCE_DRIVER_400_HEAD_PACKAGES:
        # These model-specific ReferenceDriver cartridges declare 400 nozzles at
        # 300 dpi for every channel. The generic inherited Gutenprint profile
        # instead has 144 nozzles at a different pitch.
        modes = tuple(replace(mode, nozzle_count=400, nozzle_separation=1)
                      for mode in modes)
    elif package.package_id == "pf--Epson_SureColorF100":
        # F100 has 128 colour nozzles at 300 dpi and a 400-nozzle black bank.
        # Use the colour bank for host weave; the black bank starts 272 nozzle
        # positions farther down the same 300-dpi grid.
        modes = tuple(replace(mode, nozzle_count=128, nozzle_separation=1)
                      for mode in modes)
    if not modes:
        raise ValueError(
            f"Imported printer has no modes for standard media: {package.package_id}"
        )
    ink_configurations = _ink_configurations(backend_runtime)
    if not pages:
        raise ValueError(f"Imported printer has no page sizes: {package.package_id}")
    driver_mechanics = (source.get("driver") or {}).get("driver_mechanics") or {}
    physical_paths = _physical_paths(package, source, backend_runtime)
    if package.package_id == "pf--Epson_SureColorF100":
        physical_paths = tuple(
            replace(path, escp2_selector=0, additional_escp2_selectors=(),
                    head_offset=path.head_offset + 272)
            if path.factory_role == "black" else path
            for path in physical_paths
        )
    input_sources = _feeds(
        source, backend_runtime,
        prefer_model_feeds=package.package_id in _MODEL_SPECIFIC_FEED_PACKAGES,
        include_product_roll_feed=package.package_id == "pf--Epson_SureColorP800",
    )
    if package.package_id == "pf--Epson_SureColorP800":
        # P800 captures: top PP 01 00, fine-art PP 02 02, board PP 02 00,
        # roll PP 03 00 with EX's continuous-feed flag. Preserve saved feed IDs.
        routes = {"Standard": (1, 0, "Top Feed"),
                  "Velvet": (2, 2, "Front Fineart"),
                  "Matte": (2, 0, "Front Board"),
                  "Roll_Paper": (3, 0, "Roll Paper")}
        input_sources = tuple(replace(
            feed, path_type=routes[feed.identity][0],
            path_number=routes[feed.identity][1], display_name=routes[feed.identity][2],
            initialization_commands=tuple(
                (command, bytes((0, *routes[feed.identity][:2])))
                if command == "PP" else (command, payload)
                for command, payload in feed.initialization_commands
            ) if feed.identity != "Roll_Paper" else (
                ("EX", bytes.fromhex("000000000501")),
                ("PP", bytes.fromhex("000300")),
            ),
        ) for feed in input_sources)
    if package.package_id in {"gp--escp2-r2880", "gp--escp2-r3000"}:
        input_sources = tuple(feed for feed in input_sources if feed.identity != "CD")
    return PrinterModel(
        identity=package.model_id,
        display_name=package.display_name,
        backend_family=package.backend_family,
        physical_paths=physical_paths,
        modes=modes,
        media=media,
        pages=pages,
        input_sources=input_sources,
        ppd=ppd,
        source_summary=(package.package_id, package.ppd_source, *package.warnings),
        media_aliases=media_aliases,
        native_media_page_overrides=(),
        device_settings=device_settings,
        ink_configurations=ink_configurations,
        default_ink_configuration=(
            ink_configurations[0].identity
            if ink_configurations
            else None
        ),
        escp2_profile=profile,
        catalog_package_id=package.package_id,
        command_level=(
            int(driver_mechanics["command_level"])
            if "command_level" in driver_mechanics
            else None
        ),
        mechanical_feature_set=(
            int(driver_mechanics["mechanical_feature_set"])
            if "mechanical_feature_set" in driver_mechanics
            else None
        ),
        leading_feed_offset_mils=int(
            driver_mechanics.get("leading_feed_offset_mils", 0)
        ),
        raster_origin_at_page_edge=(
            package.package_id in _PAGE_EDGE_RASTER_ORIGIN_PACKAGES
        ),
        base_separation_dpi=(
            300 if package.package_id in _REFERENCE_DRIVER_400_HEAD_PACKAGES
            or package.package_id == "pf--Epson_SureColorF100"
            else int(backend_runtime.get("base_separation_dpi", 360))
        ),
        escp2_command_set=backend_runtime.get("command_set"),
        escp2_unit_scale=int(backend_runtime.get("unit_scale", 5760)),
        escp2_page_margins=tuple(
            (str(item["interleave"]), str(item["media"]), tuple(float(x) for x in item["points"]))
            for item in backend_runtime.get("page_margins", ())
        ),
    )


def load_visible_catalog_models(
    root: Path = CATALOG_ROOT,
) -> tuple[PrinterModel, ...]:
    # Visibility is a catalog choice.  A visible descriptor without a protocol
    # profile still cannot be instantiated as a working printer model.
    return tuple(
        load_catalog_model(item, root)
        for item in load_catalog_packages(root)
        if item.visible and item.backend_status == "IMPLEMENTED"
    )
