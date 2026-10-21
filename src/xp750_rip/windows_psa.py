# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Windows Print Support App capability export and safe job-record bridge.

This module is deliberately a configuration boundary.  It never decodes PDL,
renders a raster, creates a PRN stream, or opens a printer transport.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path, PureWindowsPath
from typing import TYPE_CHECKING
from xml.etree import ElementTree
from xml.sax.saxutils import escape

from PIL import Image, ImageOps

from xp750_rip.direction import PrintDirection, parse_print_direction
from xp750_rip.dither.strategy import HalftoneAlgorithm, require_implemented_algorithm
from xp750_rip.halftone import LogicalDotPlane
from xp750_rip.layout import LayoutOrientation, ResamplingMethod, _validate_request
from xp750_rip.printers.capabilities import PageCapability, PrinterRegistry
from xp750_rip.printers.xp750_capabilities import default_registry
from xp750_rip.qtr.runtime_controls import (
    QTR_GAMMA_MAX,
    QTR_GAMMA_MIN,
    QTR_INK_LIMIT_MAX,
    QTR_INK_LIMIT_MIN,
)

if TYPE_CHECKING:
    from xp750_rip.application import PrintJobConfiguration

CAPABILITY_SCHEMA = "pytone-rip.capabilities"
CONFIGURATION_SCHEMA = "pytone-rip.configuration"
RIP_CONFIGURATION_SCHEMA = "pytone-rip.rip-configuration"
JOB_RECORD_SCHEMA = "pytone-rip.job-record"
BRIDGE_RESULT_SCHEMA = "pytone-rip.bridge-result"
RENDER_RESULT_SCHEMA = "pytone-rip.render-result"
SCHEMA_VERSION = 1
PDL_BOUNDARY = "PDL_INPUT_ADAPTER_NOT_IMPLEMENTED"
PDL_ADAPTER_TIFF_TOKEN = "<PDL_INPUT_ADAPTER_OUTPUT_TIFF>"


def _option_status(*, implemented: bool, blocked_reason: str | None) -> dict[str, object]:
    return {
        "implemented": implemented,
        "enabled": implemented,
        "status": "IMPLEMENTED" if implemented else "NOT_IMPLEMENTED",
        "blocked_reason": blocked_reason,
    }


def export_capabilities(registry: PrinterRegistry | None = None) -> dict[str, object]:
    """Return a JSON-safe, read-only view of the Python printer registry."""

    selected_registry = registry or default_registry()
    printers: list[dict[str, object]] = []
    for model in selected_registry.models:
        printers.append(
            {
                "id": model.identity,
                "display_name": model.display_name,
                "backend_family": model.backend_family,
                # ReferenceDriver descriptors store a separate native mode record for
                # each paper class.  Preferences presents the union of their
                # physical printer resolutions and resolves the matching
                # paper-specific record when applying the configuration.
                "media_specific_modes": any(
                    item.media_class.startswith("pf:") for item in model.modes
                ),
                "physical_paths": [
                    {
                        "id": item.identity.value,
                        "factory_role": item.factory_role,
                        "short_name": item.short_name,
                        "display_name": item.display_name,
                        "default_logical_ink": item.default_logical_ink,
                        "configuration_roles": [
                            {"configuration": configuration, "role": role}
                            for configuration, role in item.configuration_roles
                        ],
                    }
                    for item in model.physical_paths
                ],
                "ink_configurations": [
                    {
                        "id": item.identity,
                        "display_name": item.display_name,
                        "active_logical_ink": item.active_logical_ink,
                    }
                    for item in model.ink_configurations
                ],
                "default_ink_configuration": model.default_ink_configuration,
                "modes": [
                    {
                        "id": item.identity,
                        "display_name": item.display_name,
                        # Kept separately: Windows raster DPI controls XPS to
                        # TIFF conversion, while printer DPI identifies the
                        # selected native Epson quality/pass mode.
                        "resolution_dpi": [item.horizontal_dpi, item.vertical_dpi],
                        "raster_resolution_dpi": [
                            item.horizontal_dpi,
                            item.vertical_dpi,
                        ],
                        "printer_resolution_dpi": [
                            item.physical_horizontal_dpi,
                            item.physical_vertical_dpi,
                        ],
                        "output_resolution_dpi": list(
                            item.output_resolution_dpi
                            or (item.physical_horizontal_dpi, item.physical_vertical_dpi)
                        ),
                        **_option_status(
                            implemented=item.implemented,
                            blocked_reason=item.blocked_reason,
                        ),
                    }
                    for item in model.modes
                ],
                "media": [
                    {
                        "id": item.identity,
                        "display_name": item.display_name,
                        "supported_modes": list(item.supported_modes),
                        **_option_status(
                            implemented=item.implemented,
                            blocked_reason=item.blocked_reason,
                        ),
                    }
                    for item in model.media
                ],
                "media_aliases": dict(model.media_aliases),
                "pages": [
                    {
                        "id": item.ppd.name,
                        "display_name": _readable_page_label(
                            item.ppd.label, *_windows_page_dimensions_mm(item)
                        ),
                        "width_mm": _windows_page_dimensions_mm(item)[0],
                        "height_mm": _windows_page_dimensions_mm(item)[1],
                        "borderless": item.ppd.borderless,
                        # Same effective native policy as the backend,
                        # including model-level PPD capability corrections.
                        "allowed_media": [
                            medium.identity
                            for medium in model.media
                            if medium.implemented
                            and not model.media_page_constraints(medium.identity, item.ppd.name)
                        ],
                        **_option_status(
                            implemented=item.implemented,
                            blocked_reason=item.blocked_reason,
                        ),
                    }
                    for item in model.pages
                ],
                "input_sources": [
                    {
                        "id": item.identity,
                        "display_name": item.display_name,
                        **_option_status(
                            implemented=item.implemented,
                            blocked_reason=item.blocked_reason,
                        ),
                    }
                    for item in model.selectable_input_sources()
                ],
                "device_settings": [
                    {
                        "id": setting.identity,
                        "display_name": setting.display_name,
                        "default_choice": setting.default_choice,
                        "choices": [
                            {
                                "id": choice.identity,
                                "display_name": choice.display_name,
                            }
                            for choice in setting.choices
                        ],
                    }
                    for setting in model.device_settings
                ],
                "maintenance_actions": [
                    {
                        "id": action.identity,
                        "display_name": action.display_name,
                        "description": action.description,
                        "requires_paper": action.requires_paper,
                        "consumes_ink": action.consumes_ink,
                    }
                    for action in model.maintenance_actions
                ],
            }
        )

    dithers = []
    for algorithm in HalftoneAlgorithm:
        dithers.append(
            {
                "id": algorithm.value,
                "display_name": algorithm.display_name,
                **_option_status(
                    implemented=True,
                    blocked_reason=None,
                ),
            }
        )
    return {
        "schema": CAPABILITY_SCHEMA,
        "version": SCHEMA_VERSION,
        "printers": printers,
        "adjustments": {
            "gamma": {"minimum": QTR_GAMMA_MIN, "maximum": QTR_GAMMA_MAX},
            "ink_limit": {
                "minimum": QTR_INK_LIMIT_MIN,
                "maximum": QTR_INK_LIMIT_MAX,
            },
        },
        "dither": dithers,
        "layout": {
            "orientations": [item.value for item in LayoutOrientation],
            "resampling": [item.value for item in ResamplingMethod],
            "sizing_modes": ["fit", "native", "scale", "dimensions"],
            "placement_modes": ["center", "offset"],
            "directions": [item.value for item in PrintDirection],
        },
        "configuration": {
            "schema": CONFIGURATION_SCHEMA,
            "version": SCHEMA_VERSION,
        },
    }


def write_capability_export(path: Path, registry: PrinterRegistry | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(export_capabilities(registry), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


_STANDARD_PAGE_OPTIONS = {
    # A standard Print Schema keyword must use the canonical media dimensions,
    # not the rounded PostScript-point dimensions found in some PPD files.
    "A3": ("ISOA3", 297_000, 420_000),
    "A4": ("ISOA4", 210_000, 297_000),
    "A5": ("ISOA5", 148_000, 210_000),
    "A6": ("ISOA6", 105_000, 148_000),
    "B5": ("JISB5", 182_000, 257_000),
    "Letter": ("NorthAmericaLetter", 215_900, 279_400),
    "Legal": ("NorthAmericaLegal", 215_900, 355_600),
    "ENV10": ("NorthAmericaNumber10Envelope", 104_775, 241_300),
    "ENVC6": ("ISOC6Envelope", 114_000, 162_000),
    "Postcard": ("JapanHagakiPostcard", 100_000, 148_000),
}

_PRINT_SCHEMA_KEYWORDS_URI = (
    "http://schemas.microsoft.com/windows/2003/08/printing/printschemakeywords"
)
_PYTONERIP_PRINT_TICKET_URI = (
    "https://pytone-rip.local/printticket/2026/phase12"
)
MAX_JOB_COPIES = 99

# PPDs often contain only a small vendor-selected language subset.  These
# terms localize the remaining structural parts of paper names so every app
# language can still receive a complete PDR.  Model-specific names and sizes
# stay untouched; genuine PPD translations always take precedence.
_PDR_PAPER_TERMS = {
    "bg": ("Без полета", "Плик", "Пощенска картичка", "широк формат"),
    "cs": ("Bez okrajů", "Obálka", "Pohlednice", "široký formát"),
    "da": ("Uden kant", "Konvolut", "Postkort", "bredt format"),
    "de": ("Randlos", "Umschlag", "Postkarte", "Breitformat"),
    "el": ("Χωρίς περιθώρια", "Φάκελος", "Ταχυδρομική κάρτα", "ευρεία μορφή"),
    "es": ("Sin bordes", "Sobre", "Tarjeta postal", "formato panorámico"),
    "et": ("Ääristeta", "Ümbrik", "Postkaart", "laiformaat"),
    "fi": ("Reunaton", "Kirjekuori", "Postikortti", "laaja koko"),
    "fr": ("Sans bordure", "Enveloppe", "Carte postale", "format large"),
    "ga": ("Gan imeall", "Clúdach", "Cárta poist", "formáid leathan"),
    "hr": ("Bez obruba", "Omotnica", "Razglednica", "široki format"),
    "hu": ("Szegély nélküli", "Boríték", "Képeslap", "széles formátum"),
    "it": ("Senza bordi", "Busta", "Cartolina", "formato largo"),
    "lt": ("Be paraščių", "Vokas", "Atvirukas", "platus formatas"),
    "lv": ("Bez apmalēm", "Aploksne", "Pastkarte", "plats formāts"),
    "mt": ("Mingħajr borduri", "Envelop", "Kartolina", "format wiesa'"),
    "nl": ("Randloos", "Envelop", "Briefkaart", "breed formaat"),
    "pl": ("Bez obramowania", "Koperta", "Pocztówka", "format panoramiczny"),
    "pt": ("Sem margens", "Envelope", "Postal", "formato panorâmico"),
    "ro": ("Fără margini", "Plic", "Carte poștală", "format panoramic"),
    "ru": ("Без полей", "Конверт", "Открытка", "широкий формат"),
    "sk": ("Bez okrajov", "Obálka", "Pohľadnica", "široký formát"),
    "sl": ("Brez robov", "Ovojnica", "Razglednica", "široki format"),
    "sv": ("Kantfri", "Kuvert", "Vykort", "brett format"),
    "uk": ("Без полів", "Конверт", "Листівка", "широкий формат"),
}


_PDR_ROLL_PAGE_TERMS = {
    "bg": ("инча", "ролка"), "cs": ("palců", "role"),
    "da": ("tommer", "rulle"), "de": ("Zoll", "Rolle"),
    "el": ("ίντσες", "ρολό"), "en": ("in", "roll"),
    "es": ("pulgadas", "rollo"), "et": ("tolli", "rull"),
    "fi": ("tuumaa", "rulla"), "fr": ("pouces", "rouleau"),
    "ga": ("orlach", "rolla"), "hr": ("inča", "rola"),
    "hu": ("hüvelyk", "tekercs"), "it": ("pollici", "rotolo"),
    "lt": ("colių", "ritinys"), "lv": ("collas", "rullis"),
    "mt": ("pulzier", "romblu"), "nl": ("inch", "rol"),
    "pl": ("cali", "rolka"), "pt": ("polegadas", "rolo"),
    "ro": ("inci", "rolă"), "ru": ("дюймов", "рулон"),
    "sk": ("palcov", "rolka"), "sl": ("palcev", "rola"),
    "sv": ("tum", "rulle"), "uk": ("дюймів", "рулон"),
}


def _fallback_localized_page_label(label: str, locale: str) -> str:
    language = locale.replace("-", "_").split("_", 1)[0].lower()
    roll_size = re.fullmatch(r"(\d+) × (\d+) in \(roll\)", label)
    if roll_size and language in _PDR_ROLL_PAGE_TERMS:
        inch, roll = _PDR_ROLL_PAGE_TERMS[language]
        return f"{roll_size[1]} × {roll_size[2]} {inch} ({roll})"
    terms = _PDR_PAPER_TERMS.get(language)
    if not terms:
        return label
    borderless, envelope, postcard, wide_size = terms
    result = re.sub(r"\bBorderless\b", borderless, label, flags=re.IGNORECASE)
    result = result.replace("Envelope", envelope)
    result = result.replace("Post Card", postcard)
    return result.replace("wide size", wide_size)


def _decode_legacy_ppd_label(label: str) -> str:
    """Recover UTF-8 PPD translations that were read as Latin-1 byte text."""

    if not any("\x80" <= character <= "\xff" for character in label):
        return label
    try:
        return label.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return label


def _readable_page_label(
    label: str, width_mm: float, height_mm: float, *, fallback: str | None = None
) -> str:
    """Display real dimensions when a source PPD exposes only a page-size ID."""

    decoded = _decode_legacy_ppd_label(label)
    if not re.fullmatch(r"w\d+h\d+", decoded.strip().strip('"'), re.IGNORECASE):
        return decoded
    if fallback is not None:
        source_label = _decode_legacy_ppd_label(fallback)
        if not re.fullmatch(r"w\d+h\d+", source_label.strip().strip('"'), re.IGNORECASE):
            return source_label
    width = f"{width_mm:.1f}".rstrip("0").rstrip(".")
    height = f"{height_mm:.1f}".rstrip("0").rstrip(".")
    return f"{width} × {height} mm"


def _windows_standard_page_option(capability: PageCapability) -> tuple[str, int, int] | None:
    # B5 in imported PPDs can mean ISO (176 x 250), not JIS (182 x 257).
    # ISO B5 sheet has no matching standard keyword in our schema mapping;
    # retain the catalog ID and publish it as a custom sheet option.
    if capability.ppd.name == "B5" and (
        abs(capability.media_width_mm - 176.0) <= 1.0
        and abs(capability.media_height_mm - 250.0) <= 1.0
    ):
        return None
    return _STANDARD_PAGE_OPTIONS.get(capability.ppd.name)


def _windows_page_dimensions_mm(capability: PageCapability) -> tuple[float, float]:
    standard = _windows_standard_page_option(capability)
    if standard is not None:
        return standard[1] / 1000.0, standard[2] / 1000.0
    if capability.ppd.name == "B5" and (
        abs(capability.media_width_mm - 176.0) <= 1.0
        and abs(capability.media_height_mm - 250.0) <= 1.0
    ):
        return 176.0, 250.0
    return capability.media_width_mm, capability.media_height_mm


def _page_option_identity(page_name: str, capability=None) -> tuple[str, str]:
    """Return the PDC namespace URI and local option name for a PPD page."""

    standard = (_windows_standard_page_option(capability) if capability is not None
                else _STANDARD_PAGE_OPTIONS.get(page_name))
    if standard:
        return _PRINT_SCHEMA_KEYWORDS_URI, standard[0]
    return _PYTONERIP_PRINT_TICKET_URI, "Page_" + re.sub(
        r"[^A-Za-z0-9_.-]", "_", page_name
    )


def _paper_option_name(kind: str, identity: str) -> str:
    """Keep PDC/PDR option IDs stable across display languages."""

    return kind + "_" + re.sub(r"[^A-Za-z0-9_.-]", "_", identity)


def _windows_raster_resolutions() -> tuple[tuple[int, int], ...]:
    # PageResolution selects the Windows XpsToTiff input raster for every
    # model, independently of physical drop resolution and weave commands.
    # Retain existing IDs and the 600 DPI default for saved configurations.
    return ((300, 300), (360, 360), (600, 600), (720, 720))


def export_print_device_resources(
    registry: PrinterRegistry | None = None,
    *,
    model_id: str,
    locale: str | None = None,
) -> str:
    """Build a model PDR using full human-facing labels from its source PPD.

    Standard Microsoft namespace keys omit the URI scheme, matching Microsoft's
    PrinterPdr.xml sample. Private HTTPS namespace keys use the full URI exposed
    by PTGetPrintDeviceResources for the installed virtual printer.
    ``locale=None`` emits the PPD's base (English) labels.
    """

    model = (registry or default_registry()).get(model_id)
    lines = ['<?xml version="1.0" encoding="utf-8"?>', "<root>"]
    for capability in model.pages:
        if not capability.implemented:
            continue
        page = capability.ppd
        namespace_uri, local_name = _page_option_identity(page.name, capability)
        native_label = (
            model.ppd.localized_label(locale, "PageSize", page.name)
            if locale
            else None
        )
        label = native_label or (
            _fallback_localized_page_label(page.label, locale)
            if locale
            else page.label
        )
        label = _readable_page_label(
            label, capability.media_width_mm, capability.media_height_mm,
            fallback=page.label,
        )
        resource_namespace = (
            namespace_uri
            if namespace_uri == _PYTONERIP_PRINT_TICKET_URI
            else namespace_uri.split("://", 1)[-1]
        )
        resource_name = resource_namespace + "/" + local_name
        lines.append(
            f'  <data name="{escape(resource_name)}"><value>{escape(label)}</value></data>'
        )
    for kind, capabilities, ppd_option in (
        ("Media", model.media, "MediaType"),
        ("Input", model.selectable_input_sources(), "InputSlot"),
    ):
        for capability in capabilities:
            if not capability.implemented:
                continue
            native_label = (
                model.ppd.localized_label(locale, ppd_option, capability.identity)
                if locale else None
            )
            label = _decode_legacy_ppd_label(native_label or capability.display_name)
            resource_name = (
                _PYTONERIP_PRINT_TICKET_URI + "/"
                + _paper_option_name(kind, capability.identity)
            )
            lines.append(
                f'  <data name="{escape(resource_name)}"><value>{escape(label)}</value></data>'
            )
    for x_dpi, y_dpi in _windows_raster_resolutions():
        lines.append(
            '  <data name="https://pytone-rip.local/printticket/2026/phase12/'
            f'Dpi{x_dpi}x{y_dpi}"><value>{x_dpi} x {y_dpi} DPI</value></data>'
        )
    lines.append("</root>")
    return "\n".join(lines) + "\n"


def export_print_device_capabilities(
    registry: PrinterRegistry | None = None,
    *,
    model_id: str | None = None,
    fallback_only: bool = False,
) -> str:
    """Build a virtual-printer PDC from one model or the safe free-slot fallback."""

    selected_registry = registry or default_registry()
    pages: dict[str, object] = {}
    # Windows rasterization resolution and the physical Epson print mode are
    # deliberately independent.  The former controls XpsToTiff input; the
    # latter remains a backend choice such as 1440x1440ov.
    raster_resolutions = _windows_raster_resolutions()
    if model_id is not None:
        models = (selected_registry.get(model_id),)
    else:
        models = selected_registry.models
    for model in models:
        for page in model.pages:
            # Free-slot manifest PDCs expose only the safe A4 bootstrap.
            # Borderless PPD variants are published only by the assigned
            # model-specific PDC after the runtime refresh callback.
            if model_id is None and page.ppd.borderless:
                continue
            if page.implemented and (not fallback_only or page.ppd.name == "A4"):
                existing = pages.setdefault(page.ppd.name, page)
                # Standard Print Schema names have authoritative metric
                # dimensions below. PPDs encode those sizes in rounded
                # PostScript points, so A4 can legitimately appear as e.g.
                # 209.903, 209.973 or 210.000 mm. Treating those harmless
                # round trips as different pages made the shared bootstrap
                # PDC impossible to generate. / Для стандартного имени размер
                # задаёт Print Schema; различия округления PPD не являются
                # конфликтом. Для нестандартных имён строгая проверка остаётся.
                if page.ppd.name not in _STANDARD_PAGE_OPTIONS and (
                    round(existing.ppd.width_mm * 1000) != round(page.ppd.width_mm * 1000)
                    or round(existing.ppd.height_mm * 1000) != round(page.ppd.height_mm * 1000)
                ):
                    raise ValueError(f"conflicting PDC dimensions for page {page.ppd.name}")
                if page.physical_media_size_mm and not existing.physical_media_size_mm:
                    pages[page.ppd.name] = page

    lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<psf2:PrintDeviceCapabilities',
        '  xmlns:psf2="http://schemas.microsoft.com/windows/2013/12/printing/printschemaframework2"',
        '  xmlns:psf="http://schemas.microsoft.com/windows/2003/08/printing/printschemaframework"',
        '  xmlns:psk="http://schemas.microsoft.com/windows/2003/08/printing/printschemakeywords"',
        '  xmlns:psk12="http://schemas.microsoft.com/windows/2013/12/printing/printschemakeywordsv12"',
        '  xmlns:ptr="https://pytone-rip.local/printticket/2026/phase12"',
        '  xmlns:xsd="http://www.w3.org/2001/XMLSchema"',
        '  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">',
        '  <psk:JobCopiesAllDocuments psf2:psftype="ParameterDef" psf2:deviceProcessed="true">',
        '    <psf:DataType psf2:psftype="Property" xsi:type="xsd:QName">xsd:integer</psf:DataType>',
        '    <psf:DefaultValue psf2:psftype="Property" xsi:type="xsd:integer">1</psf:DefaultValue>',
        f'    <psf:MaxValue psf2:psftype="Property" xsi:type="xsd:integer">{MAX_JOB_COPIES}</psf:MaxValue>',
        '    <psf:MinValue psf2:psftype="Property" xsi:type="xsd:integer">1</psf:MinValue>',
        '    <psf:Multiple psf2:psftype="Property" xsi:type="xsd:integer">1</psf:Multiple>',
        '    <psf:Mandatory psf2:psftype="Property" xsi:type="xsd:QName">psk:Unconditional</psf:Mandatory>',
        '    <psf:UnitType psf2:psftype="Property" xsi:type="xsd:string">copies</psf:UnitType>',
        '  </psk:JobCopiesAllDocuments>',
        '  <psk:DocumentCollate psf2:psftype="Feature" psf2:deviceProcessed="true">',
        '    <psk:Uncollated psf2:psftype="Option" psf2:default="true" />',
        '    <psk:Collated psf2:psftype="Option" />',
        '  </psk:DocumentCollate>',
        '  <psk:PageMediaSize psf2:psftype="Feature">',
    ]
    ordered_pages = sorted(pages.values(), key=lambda item: (item.ppd.name != "A4", item.ppd.name))
    for capability in ordered_pages:
        page = capability.ppd
        standard = _windows_standard_page_option(capability)
        namespace_uri, local_name = _page_option_identity(page.name, capability)
        prefix = "psk" if namespace_uri == _PRINT_SCHEMA_KEYWORDS_URI else "ptr"
        default = ' psf2:default="true"' if page.name == "A4" else ""
        # A standard keyword must always carry its canonical Windows metric
        # dimensions, including in the unassigned-slot bootstrap PDC.
        use_canonical_dimensions = standard is not None
        width = (
            standard[1]
            if use_canonical_dimensions
            else round(_windows_page_dimensions_mm(capability)[0] * 1000)
        )
        height = (
            standard[2]
            if use_canonical_dimensions
            else round(_windows_page_dimensions_mm(capability)[1] * 1000)
        )
        page_display_name = _readable_page_label(
            page.label, capability.media_width_mm, capability.media_height_mm
        )
        lines.extend(
            [
                f'    <{prefix}:{local_name} psf2:psftype="Option"{default}>',
                *(
                    [
                        (
                            '      <psk:DisplayName psf2:psftype="Property" '
                            f'xsi:type="xsd:string">{escape(page_display_name)}</psk:DisplayName>'
                        )
                    ]
                    if prefix == "ptr"
                    else []
                ),
                (
                    '      <psk12:PortraitImageableSize psf2:psftype="Property" '
                    f'xsi:type="psf2:ImageableAreaType">0,0,{width},{height}</psk12:PortraitImageableSize>'
                ),
                (
                    '      <psk:MediaSizeHeight psf2:psftype="ScoredProperty" '
                    f'xsi:type="xsd:integer">{height}</psk:MediaSizeHeight>'
                ),
                (
                    '      <psk:MediaSizeWidth psf2:psftype="ScoredProperty" '
                    f'xsi:type="xsd:integer">{width}</psk:MediaSizeWidth>'
                ),
                f'    </{prefix}:{local_name}>',
            ]
        )
    lines.append('  </psk:PageMediaSize>')
    if any(page.ppd.borderless for page in pages.values()):
        lines.extend([
            '  <psk:PageBorderless psf2:psftype="Feature">',
            '    <psk:None psf2:psftype="Option" psf2:default="true" />',
            '    <psk:Borderless psf2:psftype="Option" />',
            '  </psk:PageBorderless>',
        ])
    if model_id is not None:
        model = models[0]
        default_page = "A4" if "A4" in pages else ordered_pages[0].ppd.name
        for feature, kind, capabilities, ppd_option in (
            ("PageMediaType", "Media", model.media, "MediaType"),
            ("JobInputBin", "Input", model.selectable_input_sources(), "InputSlot"),
        ):
            available = [item for item in capabilities if item.implemented]
            if not available:
                continue
            defaults = (
                [item for item in available if not model.media_page_constraints(
                    item.identity, default_page)]
                if kind == "Media" else available
            ) or available
            try:
                ppd_default = model.ppd.option(ppd_option).default
            except KeyError:
                ppd_default = None
            selected = next(
                (item for item in defaults if item.identity.casefold() ==
                 (ppd_default or "").casefold()),
                None,
            )
            if selected is None:
                selected = next(
                    (item for item in defaults if "plain" in item.identity.casefold()),
                    defaults[0],
                )
            lines.append(f'  <psk:{feature} psf2:psftype="Feature">')
            for item in available:
                default = ' psf2:default="true"' if item is selected else ""
                lines.append(
                    f'    <ptr:{_paper_option_name(kind, item.identity)} '
                    f'psf2:psftype="Option"{default} />'
                )
            lines.append(f'  </psk:{feature}>')
    lines.extend(
        [
            '  <psk:PageOrientation psf2:psftype="Feature">',
            '    <psk:Portrait psf2:psftype="Option" psf2:default="true" />',
            '    <psk:Landscape psf2:psftype="Option" />',
            '  </psk:PageOrientation>',
            '  <psk:PageResolution psf2:psftype="Feature">',
        ]
    )
    for x_dpi, y_dpi in raster_resolutions:
        default = ' psf2:default="true"' if (x_dpi, y_dpi) == (600, 600) else ""
        name = f"Dpi{x_dpi}x{y_dpi}"
        lines.extend(
            [
                f'    <ptr:{name} psf2:psftype="Option"{default}>',
                (
                    '      <psk:ResolutionX psf2:psftype="ScoredProperty" '
                    f'xsi:type="xsd:integer">{x_dpi}</psk:ResolutionX>'
                ),
                (
                    '      <psk:ResolutionY psf2:psftype="ScoredProperty" '
                    f'xsi:type="xsd:integer">{y_dpi}</psk:ResolutionY>'
                ),
                f'    </ptr:{name}>',
            ]
        )
    lines.extend(
        [
            '  </psk:PageResolution>',
            # PageOutputColor=Monochrome makes the Windows XpsToTiff
            # converter emit a bilevel raster on the tested Windows 11 VM,
            # even when the scored bit depth is 8.  Keep the system-rendered
            # boundary full-tone; normalize_adapter_tiff performs the
            # intentional RGB -> 8-bit grayscale conversion afterwards.
            '  <psk:PageOutputColor psf2:psftype="Feature">',
            '    <psk:Color psf2:psftype="Option" psf2:default="true">',
            (
                '      <psk:DeviceBitsPerPixel psf2:psftype="ScoredProperty" '
                'xsi:type="xsd:integer">24</psk:DeviceBitsPerPixel>'
            ),
            (
                '      <psk:DriverBitsPerPixel psf2:psftype="ScoredProperty" '
                'xsi:type="xsd:integer">24</psk:DriverBitsPerPixel>'
            ),
            '    </psk:Color>',
            '  </psk:PageOutputColor>',
            '</psf2:PrintDeviceCapabilities>',
        ]
    )
    return "\n".join(lines) + "\n"


def write_print_device_capabilities(
    path: Path,
    registry: PrinterRegistry | None = None,
    *,
    model_id: str | None = None,
    fallback_only: bool = False,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        export_print_device_capabilities(
            registry, model_id=model_id, fallback_only=fallback_only
        ),
        encoding="utf-8",
    )


def write_model_print_device_capabilities(
    directory: Path, registry: PrinterRegistry | None = None
) -> None:
    """Write one immutable packaged PDC per registered backend model plus an index."""

    selected_registry = registry or default_registry()
    directory.mkdir(parents=True, exist_ok=True)
    # PDR was removed from the package contract. Remove legacy generated
    # resources so an incremental build cannot accidentally package them.
    for stale_pdr in directory.glob("*.pdr.xml"):
        stale_pdr.unlink()
    index: dict[str, str] = {}
    used_names: set[str] = set()
    for model in selected_registry.models:
        # Must be identical to the token used by the Windows manager and the
        # package factory when they resolve ``model.<token>.pdc.xml``.
        token = re.sub(r"[^A-Za-z0-9]", "", model.identity)
        if not token or not token[0].isalpha():
            token = "Model" + token
        if len(token) > 48:
            raise ValueError(
                f"model ID is too long for a Windows package token: {model.identity}"
            )
        filename = f"model.{token}.pdc.xml"
        folded = filename.casefold()
        if folded in used_names:
            raise ValueError(f"conflicting model PDC filename for {model.identity}")
        used_names.add(folded)
        write_print_device_capabilities(
            directory / filename, selected_registry, model_id=model.identity
        )
        index[model.identity] = filename
    # Registry normalization can replace duplicate marketing aliases with one
    # canonical model identity. Do not leave their obsolete generated PDCs in
    # the application package after an incremental regeneration.
    for stale_pdc in directory.glob("model.*.pdc.xml"):
        if stale_pdc.name.casefold() not in used_names:
            stale_pdc.unlink()
    (directory / "index.json").write_text(
        json.dumps(
            {
                "schema": "pytone-rip.model-pdc-index",
                "version": 1,
                "models": index,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _mapping(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError(f"{context} must be a JSON object")
    return dict(value)


def _exact_keys(
    value: Mapping[str, object],
    *,
    required: set[str],
    optional: set[str] = frozenset(),
    context: str,
) -> None:
    missing = required - set(value)
    unknown = set(value) - required - optional
    if missing:
        raise ValueError(f"{context} missing fields: {', '.join(sorted(missing))}")
    if unknown:
        raise ValueError(f"{context} contains unknown fields: {', '.join(sorted(unknown))}")


def _string(value: object, context: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"{context} must be a non-empty trimmed string")
    return value


def _integer(value: object, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{context} must be an integer")
    return value


def _number_or_none(value: object, context: str) -> int | float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{context} must be a number or null")
    return value


def _string_list(value: object, context: str, *, minimum: int = 0) -> list[str]:
    if not isinstance(value, list):
        raise TypeError(f"{context} must be a JSON array")
    result = [_string(item, f"{context}[]") for item in value]
    if len(result) < minimum:
        raise ValueError(f"{context} requires at least {minimum} item(s)")
    if len(result) != len(set(result)):
        raise ValueError(f"{context} must contain unique values")
    return result


def _weight_list(value: object, context: str) -> list[int]:
    if not isinstance(value, list):
        raise TypeError(f"{context} must be a JSON array")
    return [_integer(item, f"{context}[]") for item in value]


def _migrate_interchangeable_black_routes(
    model_id: str,
    logical_inks: list[str],
    routing: dict[str, str],
    ink_configuration: str | None,
) -> tuple[list[str], dict[str, str]]:
    """Migrate legacy black aliases only for a single black raster path.

    Independent banks, such as P900 PK and MK, have separate QUAD curves.
    Their names and user-selected physical assignments must remain intact.
    """

    black_aliases = {"PK", "MK", "PBK", "BK"}
    if not black_aliases.intersection(logical_inks):
        return logical_inks, routing
    try:
        model = default_registry().get(model_id)
        selected_configuration = model.ink_configuration(ink_configuration)
    except KeyError:
        return logical_inks, routing
    black_paths = {
        path.identity.value for path in model.physical_paths
        if (path.short_name or "").upper() in {"K", "PK", "MK", "PBK", "BK", "PK/MK"}
        or (path.factory_role or "").casefold().replace("_", "")
        in {"k", "black", "photoblack", "matteblack", "pbk", "bk"}
    }
    if len(black_paths) > 1:
        return logical_inks, routing
    present_aliases = [
        item for item in logical_inks
        if item in black_aliases and routing.get(item) in black_paths
    ]
    if not present_aliases:
        return logical_inks, routing

    route = routing.get("K")
    active_alias = (
        selected_configuration.active_logical_ink
        if selected_configuration is not None
        else None
    )
    if route is None and active_alias is not None:
        route = routing.get(active_alias)
    if route is None:
        route = next((routing.get(item) for item in present_aliases if routing.get(item)), None)

    migrated_inks: list[str] = []
    inserted_k = "K" in logical_inks
    for item in logical_inks:
        if item in present_aliases:
            if not inserted_k:
                migrated_inks.append("K")
                inserted_k = True
            continue
        migrated_inks.append(item)
    migrated_routing = {
        key: value for key, value in routing.items() if key not in present_aliases
    }
    if route is not None:
        migrated_routing["K"] = route
    return migrated_inks, migrated_routing


def _bool(value: object, context: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{context} must be boolean")
    return value


def configuration_to_argument_tokens(configuration: Mapping[str, object]) -> list[str]:
    """Map one versioned PSA configuration to argv tokens, never a shell string."""

    root = dict(configuration)
    _exact_keys(
        root,
        required={
            "schema",
            "version",
            "printer",
            "profiles",
            "adjustments",
            "channels",
            "layout",
        },
        optional={"connection", "calibration", "delivery_mode"},
        context="configuration",
    )
    if root["schema"] != RIP_CONFIGURATION_SCHEMA or root["version"] != SCHEMA_VERSION:
        raise ValueError("unsupported pyToneRIP RIP configuration schema/version")
    if "delivery_mode" in root and root["delivery_mode"] not in {"online", "strict_offline"}:
        raise ValueError("delivery_mode must be online or strict_offline")

    printer = _mapping(root["printer"], "printer")
    _exact_keys(
        printer,
        required={"model", "mode", "media", "page", "input_source", "direction"},
        optional={
            "allow_unsupported_ppd_combinations",
            "device_settings",
            "ink_configuration",
        },
        context="printer",
    )
    calibration = None
    if printer.get("ink_configuration") is not None:
        # Ink configuration is persistent physical-state metadata. Printers
        # such as P600/P800 expose PK and MK through one raster selector, so no
        # invented ESC command is emitted here.
        _string(printer["ink_configuration"], "printer.ink_configuration")
    if "calibration" in root:
        calibration = _mapping(root["calibration"], "calibration")
        _exact_keys(calibration, required={"enabled", "mode", "ink_limit"}, context="calibration")
        if not _bool(calibration["enabled"], "calibration.enabled"):
            calibration = None
        elif _string(calibration["mode"], "calibration.mode") not in {"rgb_selectors", "per_channel"}:
            raise ValueError("calibration.mode must be rgb_selectors or per_channel")
        elif not 0 <= _integer(calibration["ink_limit"], "calibration.ink_limit") <= 100:
            raise ValueError("calibration.ink_limit must be in range 0..100")
    profiles = _mapping(root["profiles"], "profiles")
    _exact_keys(
        profiles,
        required={
            "curves",
            "blend",
            "split_tone",
            "highlights",
            "midtones",
            "shadows",
        },
        context="profiles",
    )
    adjustments = _mapping(root["adjustments"], "adjustments")
    _exact_keys(
        adjustments, required={"gamma", "ink_limit", "dither"}, context="adjustments"
    )
    channels = _mapping(root["channels"], "channels")
    _exact_keys(channels, required={"logical_inks", "routing"}, context="channels")
    layout = _mapping(root["layout"], "layout")
    _exact_keys(
        layout,
        required={
            "sizing",
            "width_mm",
            "height_mm",
            "scale_percent",
            "orientation",
            "placement",
            "x_mm",
            "y_mm",
            "resampling",
        },
        optional={"apply_rip_layout"},
        context="layout",
    )
    if "connection" in root:
        connection = _mapping(root["connection"], "connection")
        _exact_keys(
            connection,
            required={"kind", "port"},
            optional={"host", "usb_port", "usb_device_id", "printer_queue", "usb_transport_mode"},
            context="connection",
        )
        connection_kind = _string(connection["kind"], "connection.kind")
        connection_port = _integer(connection["port"], "connection.port")
        if connection_kind == "network":
            _string(connection.get("host"), "connection.host")
            if not 1 <= connection_port <= 65535:
                raise ValueError("connection.port must be in range 1..65535")
        elif connection_kind == "usb":
            if connection.get("usb_port") is not None:
                _string(connection["usb_port"], "connection.usb_port")
            for key in ("usb_device_id", "printer_queue"):
                if connection.get(key) is not None:
                    _string(connection[key], "connection." + key)
            if connection.get("usb_transport_mode") is not None:
                mode = _string(connection["usb_transport_mode"], "connection.usb_transport_mode")
                if mode not in {"usbprint", "windows_queue"}:
                    raise ValueError("connection.usb_transport_mode must be usbprint or windows_queue")
        else:
            raise ValueError("connection.kind must be network or usb")

    logical_inks = _string_list(channels["logical_inks"], "channels.logical_inks", minimum=1)
    routing_raw = _mapping(channels["routing"], "channels.routing")
    routing = {
        _string(key, "channels.routing key"): _string(value, f"channels.routing.{key}")
        for key, value in routing_raw.items()
    }
    if set(routing) != set(logical_inks):
        raise ValueError("channels.routing must map every and only selected logical ink")
    logical_inks, routing = _migrate_interchangeable_black_routes(
        _string(printer["model"], "printer.model"),
        logical_inks,
        routing,
        (
            _string(printer["ink_configuration"], "printer.ink_configuration")
            if printer.get("ink_configuration") is not None
            else None
        ),
    )
    if set(routing) != set(logical_inks):
        raise ValueError("migrated channels.routing is incomplete")

    tokens = ["render", PDL_ADAPTER_TIFF_TOKEN]
    if calibration:
        tokens.extend(("--calibration-mode", _string(calibration["mode"], "calibration.mode")))
        tokens.extend(("--calibration-ink-limit", str(_integer(calibration["ink_limit"], "calibration.ink_limit"))))
    if _bool(
        printer.get("allow_unsupported_ppd_combinations", False),
        "printer.allow_unsupported_ppd_combinations",
    ):
        tokens.append("--allow-unsupported-ppd-combinations")
    # QUAD profiles and their blend weights are not calibration inputs. Gamma
    # and ink limit remain active without opening any saved curve files.
    if calibration is None:
        curves = _string_list(profiles["curves"], "profiles.curves", minimum=1)
        if len(curves) > 3:
            raise ValueError("profiles.curves supports at most three profiles")
        blend = _weight_list(profiles["blend"], "profiles.blend")
        highlights = _weight_list(profiles["highlights"], "profiles.highlights")
        midtones = _weight_list(profiles["midtones"], "profiles.midtones")
        shadows = _weight_list(profiles["shadows"], "profiles.shadows")
        split_tone = _bool(profiles["split_tone"], "profiles.split_tone")
        for index, curve in enumerate(curves, start=1):
            tokens.extend((f"--curve{index}", curve))
        tokens.extend(("--blend", ",".join(str(item) for item in blend)))
        tokens.append("--split-tone" if split_tone else "--no-split-tone")
        if split_tone:
            tokens.extend(("--blend-highlights", ",".join(str(item) for item in highlights)))
            tokens.extend(("--blend-midtones", ",".join(str(item) for item in midtones)))
            tokens.extend(("--blend-shadows", ",".join(str(item) for item in shadows)))
    tokens.extend(("--gamma", str(_integer(adjustments["gamma"], "adjustments.gamma"))))
    tokens.extend(
        ("--ink-limit", str(_integer(adjustments["ink_limit"], "adjustments.ink_limit")))
    )
    tokens.extend(("--dither", _string(adjustments["dither"], "adjustments.dither")))
    # Saved configurations from releases up to .97 may contain "auto".  That
    # value always emitted bidirectional ESC U 0, so migrate it at the boundary.
    direction = parse_print_direction(
        _string(printer["direction"], "printer.direction")
    ).value
    for flag, key in (
        ("--printer", "model"),
        ("--mode", "mode"),
        ("--media", "media"),
        ("--page", "page"),
        ("--input-source", "input_source"),
    ):
        tokens.extend((flag, _string(printer[key], f"printer.{key}")))
    if printer.get("ink_configuration") is not None:
        tokens.extend(
            (
                "--ink-configuration",
                _string(printer["ink_configuration"], "printer.ink_configuration"),
            )
        )
    tokens.extend(("--direction", direction))
    device_settings = _mapping(
        printer.get("device_settings", {}), "printer.device_settings"
    )
    for setting_id, choice_id in sorted(device_settings.items()):
        normalized_setting = _string(setting_id, "printer.device_settings key")
        normalized_choice = _string(
            choice_id, f"printer.device_settings.{setting_id}"
        )
        tokens.extend(
            (
                "--device-setting",
                f"{normalized_setting}={normalized_choice}",
            )
        )
    tokens.extend(("--inks", ",".join(logical_inks)))
    for logical in logical_inks:
        tokens.extend(("--map", f"{logical}={routing[logical]}"))

    sizing = _string(layout["sizing"], "layout.sizing")
    width_mm = _number_or_none(layout["width_mm"], "layout.width_mm")
    height_mm = _number_or_none(layout["height_mm"], "layout.height_mm")
    scale_percent = _number_or_none(layout["scale_percent"], "layout.scale_percent")
    if sizing == "fit":
        tokens.append("--fit")
    elif sizing == "native":
        tokens.append("--native-size")
    elif sizing == "scale":
        if scale_percent is None:
            raise ValueError("layout.scale sizing requires scale_percent")
        tokens.extend(("--scale-percent", str(scale_percent)))
    elif sizing == "dimensions":
        if width_mm is None and height_mm is None:
            raise ValueError("layout.dimensions sizing requires width_mm or height_mm")
        if width_mm is not None:
            tokens.extend(("--width-mm", str(width_mm)))
        if height_mm is not None:
            tokens.extend(("--height-mm", str(height_mm)))
    else:
        raise ValueError(f"unknown layout.sizing: {sizing}")
    tokens.extend(("--orientation", _string(layout["orientation"], "layout.orientation")))
    tokens.extend(("--resampling", _string(layout["resampling"], "layout.resampling")))
    placement = _string(layout["placement"], "layout.placement")
    x_mm = _number_or_none(layout["x_mm"], "layout.x_mm")
    y_mm = _number_or_none(layout["y_mm"], "layout.y_mm")
    if placement == "center":
        tokens.append("--center")
    elif placement == "offset":
        if x_mm is not None:
            tokens.extend(("--x-mm", str(x_mm)))
        if y_mm is not None:
            tokens.extend(("--y-mm", str(y_mm)))
    else:
        raise ValueError(f"unknown layout.placement: {placement}")
    return tokens


def validate_argument_tokens(tokens: list[str]) -> PrintJobConfiguration:
    """Apply existing argparse, QTR, registry, layout, and routing validation."""

    from xp750_rip.cli import build_parser
    from xp750_rip.product_cli import _resolve_job

    args = build_parser().parse_args(tokens)
    job, transport = _resolve_job(args)
    if transport is not None:
        raise AssertionError("PSA render bridge must never create a transport")
    model = job.target.model
    medium = model.medium(job.media_id)
    page = model.page(job.page_id)
    input_source = model.input_source(job.input_source_id)
    if not job.mode.implemented:
        raise ValueError(f"Mode {job.mode.identity} blocked: {job.mode.blocked_reason}")
    if not medium.implemented:
        raise ValueError(f"Media {medium.identity} blocked: {medium.blocked_reason}")
    if medium.supported_modes and job.mode.identity not in medium.supported_modes:
        raise ValueError(f"Media {medium.identity} does not support mode {job.mode.identity}")
    if not page.implemented:
        raise ValueError(f"Page {page.ppd.name} blocked: {page.blocked_reason}")
    if not input_source.implemented:
        raise ValueError(
            f"Input source {input_source.identity} blocked: {input_source.blocked_reason}"
        )
    require_implemented_algorithm(job.halftone.algorithm)
    _validate_request(job.image_size)
    runtime = job.qtr_runtime
    if job.calibration is None:
        assert runtime is not None
        unknown_inks = tuple(item for item in job.active_logical_inks if item.value not in runtime.logical_inks)
        if unknown_inks:
            raise ValueError("QUAD does not contain logical inks: " + ", ".join(item.value for item in unknown_inks))
    dummy_planes = tuple(
        LogicalDotPlane(item, 8, ((b"\x00",),)) for item in job.active_logical_inks
    )
    job.router.route(
        dummy_planes,
        available_paths=tuple(item.identity for item in model.physical_paths),
    )
    return job


def _artifact_path(record_dir: Path, value: object, context: str) -> Path:
    artifact = _mapping(value, context)
    _exact_keys(
        artifact,
        required={"file", "size_bytes", "sha256"},
        optional={"content_type"},
        context=context,
    )
    relative = Path(_string(artifact["file"], f"{context}.file"))
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"{context}.file must stay within the job record directory")
    path = record_dir / relative
    size, digest = _file_size_and_digest(path)
    if size != _integer(artifact["size_bytes"], f"{context}.size_bytes"):
        raise ValueError(f"{context} size mismatch")
    expected_hash = _string(artifact["sha256"], f"{context}.sha256").casefold()
    if digest != expected_hash:
        raise ValueError(f"{context} SHA-256 mismatch")
    return path


def _remap_curve_tokens(
    tokens: list[str], curve_root: Path
) -> tuple[list[str], list[dict[str, str]]]:
    """Map captured Windows curve paths to an explicit portable replay directory."""

    resolved_root = curve_root.resolve()
    if not resolved_root.is_dir():
        raise NotADirectoryError(f"Curve root не найден: {resolved_root}")
    files: dict[str, list[Path]] = {}
    for candidate in resolved_root.iterdir():
        if candidate.is_file():
            files.setdefault(candidate.name.casefold(), []).append(candidate.resolve())

    remapped = list(tokens)
    mappings: list[dict[str, str]] = []
    for flag in ("--curve1", "--curve2", "--curve3"):
        if flag not in remapped:
            continue
        index = remapped.index(flag) + 1
        original = remapped[index]
        basename = PureWindowsPath(original).name
        matches = files.get(basename.casefold(), [])
        if not matches:
            raise FileNotFoundError(
                f"Curve root не содержит профиль {basename!r} для {original!r}"
            )
        if len(matches) != 1:
            raise ValueError(f"Curve root содержит неоднозначный профиль: {basename}")
        replacement = str(matches[0])
        remapped[index] = replacement
        mappings.append({"captured": original, "runtime": replacement})
    return remapped, mappings


def _use_windows_raster_orientation(tokens: list[str]) -> list[str]:
    """Prevent applying a Windows PrintTicket orientation for a second time.

    ``XpsToTiff`` renders the PrintTicket onto the physical, portrait-fed page
    before the TIFF reaches Python.  The resulting adapter TIFF therefore
    already contains the requested portrait/landscape composition.  Passing
    the same orientation into the generic TIFF layout code rotates that final
    page again and makes a landscape job fit only the sheet's short side.

    Direct TIFF/CLI jobs still retain the normal ``--orientation`` behaviour;
    this override is used only when a verified Windows adapter TIFF is present.
    """

    adjusted = list(tokens)
    try:
        value_index = adjusted.index("--orientation") + 1
    except ValueError as error:
        raise ValueError("PSA render tokens are missing --orientation") from error
    if value_index >= len(adjusted):
        raise ValueError("PSA render tokens contain an incomplete --orientation")
    adjusted[value_index] = LayoutOrientation.AUTO.value
    return adjusted


def _use_composed_windows_page(tokens: list[str]) -> list[str]:
    """Select literal full-page mapping for an XpsToTiff adapter raster."""

    adjusted = _use_windows_raster_orientation(tokens)
    if "--windows-page" not in adjusted:
        adjusted.append("--windows-page")
    return adjusted


def bridge_job_record(
    record_path: Path, *, curve_root: Path | None = None
) -> dict[str, object]:
    """Validate a captured PSA record and stop at the explicit PDL boundary."""
    bridge, _ = prepare_job_record(record_path, curve_root=curve_root)
    return bridge


def prepare_job_record(
    record_path: Path, *, curve_root: Path | None = None,
) -> tuple[dict[str, object], PrintJobConfiguration]:
    """Validate once and retain the exact configuration and curve snapshots."""

    resolved_record = record_path.resolve()
    record = json.loads(resolved_record.read_text(encoding="utf-8"))
    record_map = _mapping(record, "job record")
    _exact_keys(
        record_map,
        required={
            "schema",
            "version",
            "job_id",
            "created_utc",
            "status",
            "metadata",
            "artifacts",
        },
        context="job record",
    )
    if record_map["schema"] != JOB_RECORD_SCHEMA or record_map["version"] != SCHEMA_VERSION:
        raise ValueError("unsupported pyToneRIP job record schema/version")
    artifacts = _mapping(record_map["artifacts"], "artifacts")
    metadata = _mapping(record_map["metadata"], "metadata")
    copies = _integer(metadata.get("copies", 1), "metadata.copies")
    if not 1 <= copies <= MAX_JOB_COPIES:
        raise ValueError(f"metadata.copies must be in 1..{MAX_JOB_COPIES}")
    _exact_keys(
        artifacts,
        required={"pdl", "print_ticket", "configuration"},
        optional={"adapter_tiff"},
        context="artifacts",
    )
    record_dir = resolved_record.parent
    _artifact_path(record_dir, artifacts["pdl"], "artifacts.pdl")
    print_ticket_path = _artifact_path(
        record_dir, artifacts["print_ticket"], "artifacts.print_ticket"
    )
    configuration_path = _artifact_path(
        record_dir, artifacts["configuration"], "artifacts.configuration"
    )
    adapter_tiff_path = None
    if "adapter_tiff" in artifacts:
        adapter_tiff_path = _artifact_path(
            record_dir, artifacts["adapter_tiff"], "artifacts.adapter_tiff"
        )
    configuration = _mapping(
        json.loads(configuration_path.read_text(encoding="utf-8")), "configuration"
    )
    tokens = configuration_to_argument_tokens(configuration)
    if adapter_tiff_path is not None:
        layout = _mapping(configuration["layout"], "configuration.layout")
        apply_rip_layout = _bool(
            layout.get("apply_rip_layout", False),
            "configuration.layout.apply_rip_layout",
        )
        tokens = (
            _use_windows_raster_orientation(tokens)
            if apply_rip_layout
            else _use_composed_windows_page(tokens)
        )
    curve_mappings: list[dict[str, str]] = []
    if curve_root is not None:
        tokens, curve_mappings = _remap_curve_tokens(tokens, curve_root)
    prepared_job = validate_argument_tokens(tokens)
    result = {
        "schema": BRIDGE_RESULT_SCHEMA,
        "version": SCHEMA_VERSION,
        "job_id": _string(record_map["job_id"], "job_id"),
        "configuration_validation": "PASS",
        "render_argument_tokens": tokens,
        "executable": False,
        "status": PDL_BOUNDARY,
        "print_ticket_file": str(print_ticket_path.resolve()),
        "copies": copies,
        "curve_mappings": curve_mappings,
    }
    if adapter_tiff_path is not None:
        result["adapter_tiff_file"] = str(adapter_tiff_path.resolve())
    return result, prepared_job


def write_bridge_result(record_path: Path, output: Path | None = None) -> dict[str, object]:
    result = bridge_job_record(record_path)
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return result


def _result_artifact(path: Path) -> dict[str, object]:
    resolved = path.resolve()
    size, digest = _file_size_and_digest(resolved)
    return {
        "file": str(resolved),
        "size_bytes": size,
        "sha256": digest,
    }


def _file_size_and_digest(path: Path) -> tuple[int, str]:
    """Verify large spool artifacts with bounded memory."""
    digest = sha256()
    size = 0
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def print_ticket_collated(path: Path) -> bool:
    """Read document collation; absent tickets retain the uncollated default."""
    root = ElementTree.parse(path).getroot()
    for feature in root.iter():
        if feature.tag.rsplit("}", 1)[-1] != "Feature":
            continue
        if feature.attrib.get("name", "").split(":")[-1] != "DocumentCollate":
            continue
        for option in feature:
            if option.tag.rsplit("}", 1)[-1] == "Option":
                name = option.attrib.get("name", "").split(":")[-1]
                if name not in {"Collated", "Uncollated"}:
                    raise ValueError(f"Unsupported DocumentCollate option: {name}")
                return name == "Collated"
    return False


def print_ticket_resolution(path: Path) -> tuple[float, float] | None:
    """Read PageResolution X/Y from a captured PrintTicket."""

    root = ElementTree.parse(path).getroot()
    for feature in root.iter():
        if not feature.attrib.get("name", "").endswith("PageResolution"):
            continue
        values: dict[str, float] = {}
        for property_node in feature.iter():
            name = property_node.attrib.get("name", "")
            key = next(
                (item for item in ("ResolutionX", "ResolutionY") if name.endswith(item)),
                None,
            )
            if key is None:
                continue
            value_node = next(
                (item for item in property_node.iter() if item.tag.endswith("Value")),
                None,
            )
            if value_node is not None and value_node.text:
                values[key] = float(value_node.text)
        if values.get("ResolutionX", 0) > 0 and values.get("ResolutionY", 0) > 0:
            return values["ResolutionX"], values["ResolutionY"]
    return None


def normalize_adapter_tiff(
    source: Path,
    output: Path,
    *,
    dpi: tuple[float, float] | None = None,
) -> Path:
    """Convert one Windows-converter TIFF page to the strict monochrome RIP input."""

    resolved_source = source.resolve()
    if not resolved_source.is_file():
        raise FileNotFoundError(f"Windows converter TIFF не найден: {resolved_source}")
    with Image.open(resolved_source) as image:
        if getattr(image, "n_frames", 1) != 1:
            raise ValueError("Phase 12.2 пока поддерживает только одностраничный TIFF")
        normalize_adapter_page(image, output, dpi=dpi)
    return output


def normalize_adapter_page(
    image: Image.Image, output: Path, *, dpi: tuple[float, float] | None = None,
) -> Path:
    """Normalize the current decoded frame without an intermediate color TIFF."""
    dpi_value = image.info.get("dpi")
    selected_dpi = dpi or (
        (float(dpi_value[0]), float(dpi_value[1]))
        if isinstance(dpi_value, tuple) and len(dpi_value) == 2
        else (360.0, 360.0)
    )
    oriented = ImageOps.exif_transpose(image)
    if oriented.mode in {"RGBA", "LA"} or "transparency" in oriented.info:
        rgba = oriented.convert("RGBA")
        background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        background.alpha_composite(rgba)
        grayscale = background.convert("RGB").convert("L")
    else:
        grayscale = oriented.convert("L")
    grayscale.load()
    output.parent.mkdir(parents=True, exist_ok=True)
    grayscale.save(output, format="TIFF", compression="tiff_lzw", dpi=selected_dpi)
    return output


def write_render_result(
    *,
    bridge: Mapping[str, object],
    input_tiff: Path,
    adapter_tiff: Path | None = None,
    prn_path: Path,
    manifest_path: Path | None,
    output: Path | None = None,
) -> dict[str, object]:
    """Record a completed offline PSA TIFF-to-PRN render; never send bytes."""

    artifacts: dict[str, object] = {
        "input_tiff": _result_artifact(input_tiff),
        "prn": _result_artifact(prn_path),
    }
    if adapter_tiff is not None:
        artifacts["adapter_tiff"] = (
            dict(artifacts["input_tiff"]) if adapter_tiff.resolve() == input_tiff.resolve()
            else _result_artifact(adapter_tiff)
        )
    if manifest_path is not None:
        artifacts["manifest"] = _result_artifact(manifest_path)
    result: dict[str, object] = {
        "schema": RENDER_RESULT_SCHEMA,
        "version": SCHEMA_VERSION,
        "job_id": _string(bridge.get("job_id"), "bridge.job_id"),
        "copies_requested": _integer(bridge.get("copies", 1), "bridge.copies"),
        "configuration_validation": bridge.get("configuration_validation"),
        "status": "PRN_READY_NOT_SENT",
        "transport_attempted": False,
        "artifacts": artifacts,
    }
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return result
