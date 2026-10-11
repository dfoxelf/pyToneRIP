# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Normalized XP-750 capabilities with PPD and model-96 provenance separate."""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

from xp750_rip.capabilities.ppd import PpdCapabilities, PpdChoice, read_ppd
from xp750_rip.escp2.mode import XP750_MODEL96_MODES
from xp750_rip.printers.capabilities import (
    InputSourceCapability,
    MediaCapability,
    PageCapability,
    PhysicalPathDefinition,
    PrinterMode,
    PrinterModel,
    PrinterRegistry,
)
from xp750_rip.printers.catalog_models import (
    _canonical_media,
    _flatten_aliases,
    load_visible_catalog_models,
)
from xp750_rip.printers.catalog import CATALOG_ROOT
from xp750_rip.printers.media_mappings import (
    MEDIA_MAPPINGS,
    canonical_media_choices,
    media_display_name,
    media_material_id,
    media_print_methods,
    supported_media_modes,
)
from xp750_rip.printers.xp750 import XP750_PHYSICAL_PATHS

_PACKAGES = CATALOG_ROOT / "packages"
XP750_ESCPR_PPD = _PACKAGES / "native--Epson_XP750" / "vendor.ppd"
XP760_ESCPR_PPD = _PACKAGES / "native--Epson_XP760" / "vendor.ppd"
XP960_ESCPR_PPD = _PACKAGES / "pf--Epson_XP960series" / "vendor.ppd"

_MODE_NAMES = {
    "360x180sw": "360 x 180 DPI Draft",
    "360sw": "360 DPI",
    "720x360sw": "720 x 360 DPI",
    "720sw": "720 DPI",
    "1440x720sw": "1440 x 720 DPI",
    "1440x1440ov": "1440 x 1440 DPI",
    "2880x1440sw": "2880 x 1440 DPI",
    "5760x1440sw": "5760 x 1440 DPI",
    "2880x2880sw": "2880 x 2880 DPI",
    "5760x2880sw": "5760 x 2880 DPI",
}

XP750_MODES = tuple(
    PrinterMode(
        identity=item.identity,
        display_name=_MODE_NAMES[item.identity],
        horizontal_dpi=item.printed_horizontal_dpi,
        vertical_dpi=item.printed_vertical_dpi,
        physical_horizontal_dpi=item.physical_horizontal_dpi,
        physical_vertical_dpi=item.physical_vertical_dpi,
        base_resolution_dpi=item.base_resolution_dpi,
        horizontal_passes=item.horizontal_passes,
        vertical_passes=item.vertical_passes,
        nozzle_count=item.nozzle_count,
        nozzle_separation=item.raw_nozzle_separation,
        bit_depth=item.bit_depth,
        dot_size=item.escp2_ink_type,
        drop_sizes=item.drop_sizes,
        density=item.density,
        source="gutenprint-5.3.5/src/xml/escp2/model/model_96.xml",
        implemented=True,
        offline_validated=True,
        ordered_matrix_id=item.ordered_matrix_id,
    )
    for item in XP750_MODEL96_MODES
)

_ALL_MODE_IDS = tuple(item.identity for item in XP750_MODEL96_MODES)


def _media_equivalence_key(
    _ppd: PpdCapabilities, choice: PpdChoice
) -> tuple[object, ...]:
    """Identify PPD labels that emit the same native media commands."""

    identity = choice.name
    mapping = MEDIA_MAPPINGS.get(identity)
    if mapping is None:
        return ("UNKNOWN", media_material_id(identity))
    _gutenprint_name, media_code, family = mapping
    # Page restrictions belong to page validation, not to the native media
    # selector.  Keeping them in this key made Photo Stickers and Premium
    # Glossy appear as separate media even though both emit the same
    # PaperMedia/PrintMethod combination.
    return media_code, family, supported_media_modes(identity, _ALL_MODE_IDS)


def load_xp750_model(
    ppd_path: Path | PpdCapabilities = XP750_ESCPR_PPD,
) -> PrinterModel:
    if isinstance(ppd_path, PpdCapabilities):
        ppd = ppd_path
        ppd_source = ppd.metadata.get("NickName", "Epson PPD")
    else:
        ppd = read_ppd(ppd_path)
        ppd_source = str(ppd_path)
    pages = tuple(
        PageCapability(
            ppd=page,
            implemented=True,
            offline_validated=True,
            blocked_reason=None,
        )
        for page in ppd.pages
    )
    media_items: list[MediaCapability] = []
    media_choices, media_aliases = canonical_media_choices(
        ppd.option("MediaType").choices,
        equivalence_key=lambda choice: _media_equivalence_key(ppd, choice),
    )
    for choice in media_choices:
        mapped = MEDIA_MAPPINGS.get(choice.name)
        if mapped is None:
            media_items.append(
                MediaCapability(
                    identity=choice.name,
                    display_name=media_display_name(choice.label),
                    source="Epson XP-750 ESC/P-R PPD + Gutenprint artisan.xml",
                    implemented=False,
                    blocked_reason="Native ESC/P2 media mapping остаётся UNKNOWN.",
                )
            )
            continue
        gutenprint_name, media_code, family = mapped
        supported_modes = supported_media_modes(choice.name, _ALL_MODE_IDS)
        is_cd = choice.name.startswith("CDDVD")
        media_items.append(
            MediaCapability(
                identity=choice.name,
                display_name=media_display_name(choice.label),
                source="XP-750 PPD MediaType -> model96 media/artisan.xml",
                implemented=not is_cd,
                gutenprint_name=gutenprint_name,
                blocked_reason=(
                    "CD printing requires CD feed geometry and a circular mask."
                    if is_cd
                    else None
                ),
                media_code=media_code,
                print_methods=media_print_methods(family, supported_modes),
                supported_modes=supported_modes,
            )
        )
    unique_media, native_aliases = _canonical_media(
        tuple(media_items), XP750_MODES, unique_media_codes=True
    )
    media_aliases = _flatten_aliases((*media_aliases, *native_aliases))
    input_sources = (
        InputSourceCapability(
            "LowerFront", "Main Tray", "XP-750 physical test / model96 reference PP mapping", True,
            path_type=1, path_number=1,
        ),
        InputSourceCapability(
            "UpperFront", "Photo Tray", "XP-750 companion path inferred from physical test", True,
            path_type=1, path_number=2,
        ),
        InputSourceCapability(
            "RearFeed", "Rear Feed", "Epson XP-750/760/950-family PPD PP mapping", True,
            path_type=2, path_number=0,
        ),
        InputSourceCapability(
            "CD", "Print to CD", "model96 inputslots/artisan.xml", False,
            "PP sequence известна, но CD page geometry, circular mask и feed mechanics не реализованы.",
            path_type=2, path_number=1,
        ),
    )
    return PrinterModel(
        identity="XP750",
        display_name="Epson XP-750",
        backend_family="epson_escp2_model96",
        physical_paths=tuple(
            PhysicalPathDefinition(
                item.identity,
                item.factory_role.value,
                escp2_selector=item.address.selector,
                head_offset=item.gutenprint_head_offset,
                allocation_index=item.gutenprint_allocation_index,
                short_name=item.factory_role.value,
                display_name=item.factory_role.name,
            )
            for item in XP750_PHYSICAL_PATHS
        ),
        modes=XP750_MODES,
        media=unique_media,
        pages=pages,
        input_sources=input_sources,
        ppd=ppd,
        source_summary=(
            ppd_source,
            "gutenprint-5.3.5/src/xml/printers/escp2.xml: escp2-px700fw -> model96",
            "gutenprint-5.3.5/src/xml/escp2/model/model_96.xml",
            "gutenprint-5.3.5/src/xml/escp2/media/artisan.xml",
            "gutenprint-5.3.5/src/xml/escp2/inputslots/artisan.xml",
            "gutenprint-5.3.5/src/main/print-weave.c + escp2-driver.c",
        ),
        media_aliases=media_aliases,
        catalog_package_id="native--Epson_XP750",
        # A6 glossy media support was explicitly confirmed by the XP-750
        # owner (2026-09-08). The ESC/P-R PPD forbids these pairs, but our
        # native ESC/P2 path uses independent page geometry and media codes.
        # This is a capability correction, not a hardware-validation claim.
        native_media_page_overrides=tuple(
            (item.identity, "A6") for item in unique_media
            if item.identity.startswith(("PMPHOTO_", "GLOSSYPHOTO_", "GLOSSYCAST_"))
        ),
    )


def load_xp960_model(ppd_path: Path = XP960_ESCPR_PPD) -> PrinterModel:
    """Return the XP-960 descriptor using the existing model96 raster backend."""

    xp750 = load_xp750_model(ppd_path)
    # The owner confirmed that Main and Photo PP selectors were reversed for
    # XP-950/960/970/980. ReferenceDriver's Cassette 1/2 PP labels are wrong here;
    # keep saved source IDs and change only their wire codes.
    xp950_family_path_numbers = {"LowerFront": 2, "UpperFront": 1}
    xp950_family_sources = tuple(
        replace(
            source,
            path_number=xp950_family_path_numbers[source.identity],
            source="XP-950/960/970/980 owner-confirmed tray routing (2026-10-05)",
        ) if source.identity in xp950_family_path_numbers else source
        for source in xp750.input_sources
    )
    return replace(
        xp750,
        identity="XP960",
        display_name="Epson XP-960 Series",
        native_media_page_overrides=(),
        modes=xp750.modes,
        media=xp750.media,
        pages=xp750.pages,
        input_sources=xp950_family_sources,
        source_summary=(
            str(ppd_path),
            "Existing PX700W-compatible Epson ESC/P2/model96 backend data reused",
        ),
        catalog_package_id="pf--Epson_XP960series",
    )


def load_xp760_model(ppd_path: Path = XP760_ESCPR_PPD) -> PrinterModel:
    """Use the XP-750 model96 channel path with XP-760's own PPD choices."""

    xp760 = load_xp750_model(ppd_path)
    # XP-750 and XP-760 are the same direct-channel platform.  Their Epson
    # RGB-driver PPDs expose different marketing subsets, but the native RIP
    # must not advertise different mechanics for the same printer family.
    family = load_xp750_model()
    return replace(
        xp760,
        identity="XP760",
        display_name="Epson Expression Photo XP-760",
        media=family.media,
        media_aliases=family.media_aliases,
        native_media_page_overrides=(),
        source_summary=(
            str(ppd_path),
            "XP-750 compatible model96 direct-channel backend selected for XP-760",
        ),
        catalog_package_id="native--Epson_XP760",
    )


_ET_MODEL_RE = re.compile(r"(?i)(?:^|[^a-z0-9])ET[-_ ]?(\d{4,5})(?:[^0-9]|$)")
_XP_MODEL_RE = re.compile(r"(?i)(?:^|[^a-z0-9])XP[-_ ]?(\d{3,4})(?:[^0-9]|$)")
_XP950_FAMILY_RE = re.compile(r"(?i)(?:^|[^a-z0-9])XP[-_ ]?(950|960|970|980)(?:[^0-9]|$)")


def _model_number(pattern: re.Pattern[str], model: PrinterModel) -> str | None:
    match = pattern.search(f"{model.identity} {model.display_name}")
    return match.group(1) if match else None


def _highest_advertised_dpi(model: PrinterModel) -> int:
    """Use selectable output quality, not the raster input/ESC/P2 grid."""

    advertised = (
        int(match.group(1))
        for mode in model.modes
        if (match := re.search(r"(\d+)\s*[x×]\s*\d+\s*DPI", mode.display_name, re.I))
    )
    return max(advertised, default=0)


def _canonical_catalog_models(
    catalog_source: tuple[PrinterModel, ...], xp960: PrinterModel
) -> tuple[PrinterModel, ...]:
    """Collapse physical-model aliases and normalize close Epson families."""

    xp_family: dict[str, PrinterModel] = {}
    xp_products: dict[str, list[PrinterModel]] = {}
    et_family: dict[str, list[PrinterModel]] = {}
    unrelated: list[PrinterModel] = []
    for model in catalog_source:
        xp_number = _model_number(_XP950_FAMILY_RE, model)
        if xp_number is not None:
            xp_family[xp_number] = model
            continue
        xp_number = _model_number(_XP_MODEL_RE, model)
        if xp_number is not None:
            xp_products.setdefault(xp_number, []).append(model)
            continue
        et_number = _model_number(_ET_MODEL_RE, model)
        if et_number is not None:
            et_family.setdefault(et_number, []).append(model)
            continue
        unrelated.append(model)

    # XP-950/960/970/980 are revisions of the same six-channel mechanism.
    # Keep each product's exact pages/PPD identity, but use the validated
    # model-96 modes, media commands and corrected feeds for every revision.
    # XP-960 is already the built-in entry and must not be added a second time.
    for number in ("950", "970", "980"):
        product = xp_family.get(number)
        if product is None:
            continue
        product_native = load_xp750_model(product.ppd)
        unrelated.append(
            replace(
                xp960,
                identity=product.identity,
                display_name=product.display_name,
                pages=product.pages,
                ppd=product.ppd,
                media=product_native.media,
                media_aliases=product_native.media_aliases,
                native_media_page_overrides=product_native.native_media_page_overrides,
                catalog_package_id=product.catalog_package_id,
                source_summary=(
                    *product.source_summary,
                    "XP-950/960/970/980 shared validated model-96 mechanics",
                ),
            )
        )

    # A few consumer XP products are present in both research sources. Keep
    # the complete Gutenprint mode mechanics (up to 5760 DPI) under the stable
    # product-catalog identity instead of publishing two printer names.
    for number, products in xp_products.items():
        if len(products) == 1:
            unrelated.extend(products)
            continue
        public = next(
            (
                item
                for item in products
                if (item.catalog_package_id or "").startswith("pf--")
            ),
            products[0],
        )
        preferred = min(
            products,
            key=lambda item: (
                0 if (
                    (item.catalog_package_id or "").startswith("pf--")
                    if number == "245"
                    else (item.catalog_package_id or "").startswith("gp--")
                ) else 1,
                item.identity,
            ),
        )
        unrelated.append(
            replace(
                preferred,
                identity=public.identity,
                display_name=public.display_name,
            )
        )

    # An EcoTank model can have both a Gutenprint product and a ReferenceDriver family
    # alias. Select from each model's actually available media/mode pairs:
    # ET-2550, for example, has 2880 DPI in Gutenprint but a 5760 DPI
    # PhotoPlus mode on glossy paper in its exact ReferenceDriver family descriptor.
    for number, products in et_family.items():
        preferred = min(
            products,
            key=lambda item: (
                -_highest_advertised_dpi(item),
                0
                if item.catalog_package_id == f"gp--escp2-et{number}"
                else 1
                if (item.catalog_package_id or "").startswith("gp--")
                else 2,
                item.identity,
            ),
        )
        if any((item.catalog_package_id or "").startswith("gp--") for item in products):
            preferred = replace(
                preferred,
                identity=f"EcoTank_ET{number}",
                display_name=f"Epson EcoTank ET-{number}",
            )
        unrelated.append(preferred)

    return tuple(unrelated)


def default_registry() -> PrinterRegistry:
    xp960 = load_xp960_model()
    builtins = (load_xp750_model(), load_xp760_model(), xp960)
    builtin_ids = {item.identity.casefold() for item in builtins}
    catalog_source = _canonical_catalog_models(load_visible_catalog_models(), xp960)
    catalog_source = tuple(
        replace(
            item,
            identity="Stylus_Photo_1500W",
            display_name="Epson Stylus Photo 1500W / Artisan 1430",
        )
        if item.catalog_package_id == "gp--escp2-artisan1430"
        else item
        for item in catalog_source
    )
    catalog_models = tuple(
        item
        for item in catalog_source
        if item.identity.casefold() not in builtin_ids
    )
    return PrinterRegistry(
        (*builtins, *catalog_models),
        aliases={"R3000": "Stylus_Photo_R3000"},
    )
