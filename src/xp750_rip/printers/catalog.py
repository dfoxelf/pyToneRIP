# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Persistent Epson printer-support catalog and safe import operations.

The catalog deliberately separates source discovery, implementation data and
GUI visibility. Hiding a descriptor must never destroy its implementation.

Каталог намеренно разделяет исходные данные, реализацию и видимость в GUI.
Скрытие модели никогда не должно уничтожать данные её реализации.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any

from xp750_rip.capabilities.ppd import read_ppd
from xp750_rip.printers.importer import (
    GUTENPRINT_ROOT,
    GutenprintResolver,
    GutenprintSelection,
    resolved_gutenprint_catalog_data,
)
from xp750_rip.printers.verified_reference_driver_pro_media import (
    CAPTURED_PRO_MEDIA_BY_DRIVER,
)

CATALOG_SCHEMA = "pytone-rip.printer-catalog"
CATALOG_VERSION = 1
PACKAGE_SCHEMA = "pytone-rip.printer-support-package"
PACKAGE_VERSION = 1
DEVELOPMENT_CATALOG_ROOT = Path(__file__).resolve().parents[3] / "printer_catalog"
_PACKAGED_CATALOG_ROOT = Path(str(files("xp750_rip.printers").joinpath("catalog_data")))
CATALOG_ROOT = (
    _PACKAGED_CATALOG_ROOT
    if _PACKAGED_CATALOG_ROOT.is_dir()
    else DEVELOPMENT_CATALOG_ROOT
)
DEFAULT_UNIFIED_ROOT = Path(
    os.environ.get(
        "PYTONERIP_UNIFIED_ROOT",
        str(Path(__file__).resolve().parents[3] / "printer_knowledge" / "exported_unified"),
    )
)
DEFAULT_QTR_ROOT = Path(
    os.environ.get(
        "PYTONERIP_QTR_ROOT",
        str(DEVELOPMENT_CATALOG_ROOT.parent / "reference" / "QuadToneRIP" / "QuadTone"),
    )
)

_PACKAGE_ID = re.compile(r"^(?:pf|gp|native)--[A-Za-z0-9_.+-]+$")
_MODEL_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")

# The first integer in a legacy four-field Setting choice is a descriptor-only
# type discriminator. It is NOT an Epson protocol byte. The second integer is
# the class-specific value. Across the complete Epson descriptor set inspected
# for this importer, fields three and four are always zero; their semantics are
# not proven, so they are treated as reserved and are never copied into the RIP
# catalog. A non-zero reserved field is rejected instead of being guessed.
#
# Первое число — тип настройки в исходном описателе, а не байт протокола Epson.
# Второе — полезное значение конкретной настройки. Третье и четвёртое поля у
# всех изученных Epson равны нулю; их смысл не доказан, поэтому они считаются
# reserved, не попадают в runtime и при ненулевом значении вызывают ошибку.
_DESCRIPTOR_SETTING_TYPES = {
    2: ("scan_drying_time", "Drying Time", True),
    3: ("paper_thickness", "Paper Thickness", True),
    4: ("paper_suction", "Paper Suction", True),
    5: ("eject_roller", "Eject Roller", False),
    6: ("cut_mode", "Cut Mode", False),
    7: ("print_separation_line", "Print Separation Line", False),
    8: ("save_roll_paper", "Save Paper (Roll Paper)", True),
    9: ("platen_gap", "Platen Gap", True),
    10: ("microweave_quality", "Microweave Quality", False),
    11: ("microweave_precision", "Microweave Precision Correction", True),
    12: ("black_enhance", "Black Enhance", False),
    14: ("ink_amount", "Ink Amount", False),
}


class CatalogError(ValueError):
    """Base error for deterministic catalog failures."""


class UnsafeCatalogOperation(CatalogError):
    """Raised when a path or identity fails a destructive-operation guard."""


@dataclass(frozen=True, slots=True)
class CatalogPackage:
    package_id: str
    model_id: str
    display_name: str
    protocol_class: str
    protocol_family: str
    backend_family: str
    backend_status: str
    visible: bool
    source_sha256: str
    ppd_file: str
    ppd_source: str
    runtime_source_file: str | None
    backend_source_file: str | None
    backend_runtime_file: str | None
    warnings: tuple[str, ...]

    @classmethod
    def from_json(cls, value: dict[str, Any]) -> CatalogPackage:
        if value.get("schema") != PACKAGE_SCHEMA or value.get("version") != PACKAGE_VERSION:
            raise CatalogError("Unsupported printer support package schema")
        return cls(
            package_id=str(value["package_id"]),
            model_id=str(value["model_id"]),
            display_name=str(value["display_name"]),
            protocol_class=str(value["protocol_class"]),
            protocol_family=str(value["protocol_family"]),
            backend_family=str(value["backend_family"]),
            backend_status=str(value["backend_status"]),
            visible=bool(value["visible"]),
            source_sha256=str(value["source_sha256"]),
            ppd_file=str(value["ppd_file"]),
            ppd_source=str(value["ppd_source"]),
            runtime_source_file=(
                str(value["runtime_source_file"])
                if value.get("runtime_source_file")
                else None
            ),
            backend_source_file=(
                str(value["backend_source_file"])
                if value.get("backend_source_file")
                else None
            ),
            backend_runtime_file=(
                str(value["backend_runtime_file"])
                if value.get("backend_runtime_file")
                else None
            ),
            warnings=tuple(str(item) for item in value.get("warnings", ())),
        )


# Explicit cross-source joins. Each entry was established from the driver ID
# and the matching Gutenprint model record; the importer never fuzzy-matches a
# protocol implementation from marketing text.
_BACKEND_DATA_PACKAGES = {
    "pf--Epson_SureColorP400": "gp--escp2-p400",
    "pf--Epson_SureColorP600": "gp--escp2-p600",
    "pf--Epson_SureColorP800": "gp--escp2-p800",
    "pf--Epson_XP15000series": "gp--escp2-xp15000",
    "pf--Epson_XP200series": "gp--escp2-sx420",
    "pf--Epson_XP220series": "gp--escp2-sx420",
}

_BACKEND_DATA_DRIVERS = {
    # The whole XP510/XP800 technical family uses the same model-91 head and
    # weave mechanics.  Individual product PPDs remain authoritative for page
    # sizes and media names.
    "XP510": "gp--escp2-xp820",
    "XP800": "gp--escp2-xp820",
}

_PROTOCOL_OVERRIDES = {
    "pf--Epson_SureColorT3200": ("escp2-esci", "T7200"),
    "pf--Epson_SureColorT5200": ("escp2-esci", "T7200"),
    "pf--Epson_SureColorT5400": ("escp2-esci", "SC-T5400"),
    "pf--Epson_SureColorT7200": ("escp2-esci", "T7200"),
    "pf--Epson_SureColorP400": ("escp2-esci", "gutenprint-model-91"),
    "pf--Epson_SureColorP600": ("escp2-esci", "gutenprint-model-115"),
    "pf--Epson_SureColorP800": ("escp2-esci", "gutenprint-model-130"),
    "pf--Epson_XP15000series": ("escp2-esci", "gutenprint-model-132"),
    "pf--Epson_XP200series": ("escp2-esci", "gutenprint-model-94"),
    "pf--Epson_XP220series": ("escp2-esci", "gutenprint-model-94"),
}

_PROTOCOL_DRIVER_OVERRIDES = {
    "XP510": ("escp2-esci", "gutenprint-model-91"),
    "XP800": ("escp2-esci", "gutenprint-model-91"),
}

# ReferenceDriver maps these five-cartridge printers to common XP510/XP800 technical
# drivers. Normal color-chart captures use K=00/M=01/C=02/Y=04 only, but the
# Gutenprint XP-820 raw ink definition ``PhysicalCMYKPRO`` explicitly exposes
# both standard black (color=0) and photo black (color=0, subchannel=4). Thus
# PBK is independently addressable as ESC i 0x40 and may be emitted together
# with BK 0x00; LC/LM 0x12/0x11 do not exist on this five-color mechanism.
_DIRECT_CHANNEL_OVERRIDES = {
    "T7200": (
        ("mk", "matte_black", "0x00"),
        ("pk", "photo_black", "0x40"),
        ("m", "magenta", "0x01"),
        ("c", "cyan", "0x02"),
        ("y", "yellow", "0x04"),
    ),
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
    # ReferenceDriver reuses an L8050-style six-colour descriptor for these CMYK
    # printers.  Their PPD/product records describe four installed inks.
    "ET14100": (
        ("k", "black", "0x00"),
        ("m", "magenta", "0x01"),
        ("c", "cyan", "0x02"),
        ("y", "yellow", "0x04"),
    ),
    # Glossy ReferenceDriver samples omit pigment K, while plain-paper captures and
    # the physical ink set establish the normal K address.
    "T3100": (
        ("k", "black", "0x00"),
        ("m", "magenta", "0x01"),
        ("c", "cyan", "0x02"),
        ("y", "yellow", "0x04"),
    ),
    # ET-8500/8550 and L8160/8180 have three distinct dark-ink slots:
    # pigment black, photo black and gray. ReferenceDriver's k2/k3 labels are driver
    # internals, not useful physical ink names.
    "ET8550": (
        ("k", "pigment_matte_black", "0x00"),
        ("photo_k", "photo_black", "0x10"),
        ("gray", "gray", "0x30"),
        ("m", "magenta", "0x01"),
        ("c", "cyan", "0x02"),
        ("y", "yellow", "0x04"),
    ),
}

_DIRECT_CHANNEL_PACKAGE_OVERRIDES = {
    "pf--Epson_SureColorT5400": (
        ("k", "black", "0x00"),
        ("m", "magenta", "0x01"),
        ("c", "cyan", "0x02"),
        ("y", "yellow", "0x04"),
    ),
}


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _safe_package_id(package_id: str) -> str:
    if not _PACKAGE_ID.fullmatch(package_id):
        raise UnsafeCatalogOperation(f"Unsafe unified package ID: {package_id!r}")
    return package_id


def _safe_model_id(model_id: str) -> str:
    if not _MODEL_ID.fullmatch(model_id):
        raise CatalogError(f"Invalid normalized model ID: {model_id!r}")
    return model_id


def _package_directory(root: Path, package_id: str) -> Path:
    safe = _safe_package_id(package_id)
    base = (root / "packages").resolve()
    target = (base / safe).resolve()
    if target.parent != base:
        raise UnsafeCatalogOperation("Printer package path escaped the catalog root")
    return target


def _atomic_write(path: Path, data: bytes) -> None:
    """Replace one catalog file only after a complete sibling temp write.

    Файл никогда не остаётся частично записанным: временный файл создаётся в
    том же каталоге, затем атомарно заменяет конечный путь.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _normalized_model_id(source: dict[str, Any], package_id: str) -> str:
    identity = source.get("identity") or {}
    raw = str(identity.get("name") or package_id.split("--", 1)[1])
    raw = re.sub(r"^Epson[_\s-]*", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"(?i)_RIP$", "RIP", raw)
    token = re.sub(r"[^A-Za-z0-9]+", "_", raw).strip("_")
    return _safe_model_id(token or "EpsonImported")


def _display_name(source: dict[str, Any]) -> str:
    identity = source.get("identity") or {}
    marketing = str(identity.get("marketing") or "").strip()
    raw_name = str(identity.get("name") or "").strip()
    normalized_name = re.sub(r"^Epson_?", "", raw_name, flags=re.IGNORECASE)
    # Product IDs are more reliable than a compatibility/marketing string.
    # Some descriptors named XP950series as "PX-750 Series", which created an
    # apparent duplicate of the built-in XP-750 in the GUI.
    #
    # Идентификатор модели имеет приоритет: строка совместимости не должна
    # визуально превращать XP-950 в другой принтер.
    series_match = re.fullmatch(
        r"(?P<family>XP|EcoTank_ET|SureColor[PFT])(?P<number>\d+)(?P<suffix>series|_HQ|_RIP)?",
        normalized_name,
        flags=re.IGNORECASE,
    )
    if series_match:
        family = series_match.group("family").casefold()
        number = series_match.group("number")
        suffix = (series_match.group("suffix") or "").casefold()
        if family == "xp":
            return f"Epson XP-{number}" + (" Series" if suffix == "series" else "")
        if family == "ecotank_et":
            return f"Epson EcoTank ET-{number}" + (" HQ" if suffix == "_hq" else "")
        prefix = {"surecolorp": "P", "surecolorf": "F", "surecolort": "T"}[family]
        return f"Epson SureColor {prefix}{number}"
    name = (raw_name or "Epson imported printer").replace("_", " ").strip()
    value = marketing or name
    return value if value.casefold().startswith("epson") else "Epson " + value


def _correct_protocol(
    package_id: str,
    source: dict[str, Any],
    *,
    allow_driver_override: bool = True,
) -> tuple[str, str, str, tuple[str, ...]]:
    """Normalize known research-index errors without rewriting source evidence."""

    protocol = source.get("protocol") or {}
    protocol_class = str(protocol.get("cls") or "unknown")
    family = str(protocol.get("family") or "unknown")
    warnings: list[str] = []
    driver_id = str((source.get("driver") or {}).get("id") or "")
    override = _PROTOCOL_OVERRIDES.get(package_id)
    if override is None and allow_driver_override:
        override = _PROTOCOL_DRIVER_OVERRIDES.get(driver_id)
    if override is not None:
        protocol_class, family = override
        warnings.append(
            "SOURCE_CORRECTION: protocol family resolved by an explicit compatible "
            "driver descriptor"
        )
    if package_id in {
        "pf--Epson_SureColorP700_RIP",
        "pf--Epson_SureColorP900_RIP",
    }:
        # FACT from P700/P900 _RIP captures: these packages use direct ESC i
        # channel addressing. The stale unified protocol label is retained in
        # printer.json as source evidence and corrected only in our manifest.
        protocol_class = "escp2-esci"
        family = "P700RIP"
        warnings.append(
            "SOURCE_CORRECTION: direct ESC i mode established by captured RIP streams"
        )
    family_token = re.sub(r"[^a-z0-9]+", "_", family.casefold()).strip("_")
    backend_family = (
        f"epson_escp2_{family_token}"
        if protocol_class == "escp2-esci"
        else "unsupported_" + protocol_class.replace("-", "_")
    )
    return protocol_class, family, backend_family, tuple(warnings)


def _backend_status(protocol_class: str) -> str:
    if protocol_class == "escp2-esci":
        return "IMPLEMENTED"
    return "IMPLEMENTATION_REQUIRES_PROTOCOL_PROFILE"


def _correct_direct_channels(
    source: dict[str, Any], package_id: str = "",
) -> dict[str, Any]:
    """Apply capture-proven logical channel maps omitted by ReferenceDriver's TPP."""

    driver_id = str((source.get("driver") or {}).get("id") or "")
    override = _DIRECT_CHANNEL_PACKAGE_OVERRIDES.get(package_id)
    if override is None:
        override = _DIRECT_CHANNEL_OVERRIDES.get(driver_id)
    if override is None:
        return source
    corrected = dict(source)
    corrected["channels"] = [
        {"slot": slot, "role": role, "esc_i_code": code}
        for slot, role, code in override
    ]
    corrected["channels_note"] = (
        "ReferenceDriver captures verify ESC i 0x00/0x01/0x02/0x04; Gutenprint "
        "escp2-xp820 model 91 PhysicalCMYKPRO additionally proves the "
        "independent photo-black selector color=0, subchannel=4 (0x40)"
    )
    return corrected


def _without_color_assets(value: Any) -> Any:
    """Recursively remove color-correction payloads from B&W support data."""

    if isinstance(value, dict):
        return {
            key: _without_color_assets(item)
            for key, item in value.items()
            if "curve" not in key.casefold() and "icc" not in key.casefold()
        }
    if isinstance(value, list):
        return [_without_color_assets(item) for item in value]
    return value


_MECHANICAL_PAPER_PARAMETERS = (
    "PaperMedia",
    "PrintMethod",
    "PaperThickness",
    "VacuumIntensity",
    "FeedAdjustment",
    "FeedSequence",
    "PlatenGap",
)


def _without_paper_color_conversion(value: dict[str, Any]) -> dict[str, Any]:
    """Keep only native mechanics from technical paper parameter tables.

    Gamma, color balances, GCR, saturation and density belong to a color
    renderer. pyToneRIP uses QUAD curves and its own ink limit, so importing
    those values would silently apply a second tone/color correction.

    Из таблиц бумаги сохраняются только команды носителя и механики. Цветовые
    коэффициенты не должны вмешиваться в QUAD-кривые и Ink Limit pyToneRIP.
    """

    result = dict(value)
    papers: list[dict[str, Any]] = []
    for raw_item in value.get("paper_types") or ():
        item = dict(raw_item)
        searchable_name = (
            f"{item.get('name', '')} {item.get('text', '')}"
        ).casefold()
        if any(
            marker in searchable_name
            for marker in _NONSTANDARD_BRANDED_MEDIA_MARKERS
        ):
            continue
        parameters = item.get("params") or {}
        item["params"] = {
            key: parameters[key]
            for key in _MECHANICAL_PAPER_PARAMETERS
            if key in parameters
        }
        papers.append(item)
    if "paper_types" in value:
        result["paper_types"] = papers
    return result


def _legacy_four_fields(value: object, *, record_name: str) -> tuple[int, int, int, int]:
    """Parse one legacy four-integer record only at the import boundary.

    The positional representation is deliberately confined to this function.
    Installed packages and runtime code must use named fields, otherwise a
    future maintainer cannot tell protocol bytes from UI or color parameters.

    Позиционное представление разрешено только на границе импорта. В package и
    runtime попадают исключительно именованные поля с проверенной семантикой.
    """

    if isinstance(value, (list, tuple)):
        fields = tuple(int(item) for item in value)
    else:
        tokens = tuple(item.strip() for item in str(value or "").split(","))
        try:
            fields = tuple(int(item) for item in tokens if item != "")
        except ValueError as error:
            raise CatalogError(f"Non-integer {record_name} fields: {value!r}") from error
    if len(fields) != 4:
        raise CatalogError(
            f"{record_name} must contain exactly four integers: {value!r}"
        )
    return fields


def _decoded_driver_parameters(value: object) -> dict[str, int]:
    """Decode the printer-family command level and signed leading feed.

    ``packed_family`` uses decimal packing: thousands select the mechanical
    feature set and the last three decimal digits select the command level.
    The two following source fields form one signed offset in 1/1000 inch.

    Первое число десятично упаковывает семейство механики и уровень команд.
    Следующие два исходных поля задают одно знаковое смещение начала подачи.
    """

    packed_family, positive_offset, negative_offset, reserved = _legacy_four_fields(
        value, record_name="Driver.Param"
    )
    if min(packed_family, positive_offset, negative_offset) < 0:
        raise CatalogError("Driver parameters must be non-negative before sign decoding")
    if reserved != 0:
        raise CatalogError(f"Unknown non-zero Driver.Param reserved field: {reserved}")
    return {
        "command_level": packed_family % 1000,
        "mechanical_feature_set": packed_family // 1000,
        "leading_feed_offset_mils": (
            # The native setup gives the negative field precedence when both
            # are present (ET-2700 is the known case); this is not subtraction.
            # При наличии обоих полей используется отрицательное — это точное
            # ветвление исходной механики, а не разность двух чисел.
            -negative_offset if negative_offset else positive_offset
        ),
    }


_SHEET_GEOMETRY_CLASSES = {
    0: "standard_sheet",
    1: "photo_or_card",
    2: "envelope",
}


def _special_media(value: int, *, record_name: str) -> dict[str, object]:
    """Name the only special medium selector present in the Epson data set."""

    if value == 0:
        return {"kind": "none"}
    if value == 0x0201:
        return {"kind": "printable_disc", "selector": value}
    raise CatalogError(f"Unknown {record_name} special-media selector: {value}")


def _decoded_media_size_parameters(value: object) -> dict[str, object]:
    """Decode page-size protocol ID, geometry class and disc selector.

    Field 1 is not a borderless flag. The complete Epson table contains both
    ordinary and borderless photo/card sizes with value 1, while envelopes use
    value 2. Borderless geometry remains an independent page descriptor field.

    Поле 1 — не флаг печати без полей: значение 1 имеют также обычные A6 и
    4x6, значение 2 — конверты. Поэтому оно названо классом геометрии листа.
    """

    media_size_code, geometry_code, special_selector, reserved = _legacy_four_fields(
        value, record_name="Size.Param"
    )
    if reserved != 0:
        raise CatalogError(f"Unknown non-zero Size.Param reserved field: {reserved}")
    try:
        geometry_class = _SHEET_GEOMETRY_CLASSES[geometry_code]
    except KeyError as error:
        raise CatalogError(f"Unknown sheet geometry class: {geometry_code}") from error
    return {
        "media_size_code": media_size_code,
        "sheet_geometry_class": geometry_class,
        "special_media": _special_media(special_selector, record_name="Size.Param"),
    }


_FEED_KINDS = {
    0: "automatic_cut_sheet",
    1: "selected_cut_sheet_path",
    2: "roll",
}
_ROLL_CUT_MODES = {0: "none", 1: "cut_each_page", 2: "cut_last_page"}

_NONSTANDARD_BRANDED_MEDIA_MARKERS = (
    "hahnem",
    "ilford",
    "kodak",
    "fuji",
    "zedonet",
)


_KNOWN_MEDIA_PARAMETER_REPAIRS: dict[str, dict[str, int]] = {
    # The ET-14000 unified record lost every Paper.Param value during the
    # original ReferenceDriver export (all were written as 0,0,0,0). ET-14000 and
    # L1300 use the same ReferenceDriver B1100 driver; the intact L1300 descriptor
    # provides the native media selectors below.  Apply this only when the
    # affected record is still all-zero, so later authoritative data wins.
    "Epson_EcoTank_ET14000": {
        "plain_paper": 0,
        "epson_inkjet": 29,
        "epson_matteheavy": 5,
        "epson_glossy": 6,
        "premium_glossy": 11,
        "epson_durabrite": 11,
        "epson_transparency": 9,
    },
}


def _repair_known_media_parameters(source: dict[str, Any]) -> dict[str, Any]:
    identity = source.get("identity") or {}
    repairs = _KNOWN_MEDIA_PARAMETER_REPAIRS.get(str(identity.get("name") or ""))
    if repairs is None or str((source.get("driver") or {}).get("id") or "") != "B1100":
        return source
    records: list[dict[str, Any]] = []
    changed = False
    for raw_item in source.get("media") or ():
        item = dict(raw_item)
        match = str(item.get("match") or "")
        current = _legacy_four_fields(item.get("param"), record_name="Paper.Param")
        if match in repairs and current == (0, 0, 0, 0):
            item["param"] = f"{repairs[match]},0,0,0"
            changed = True
        records.append(item)
    if not changed:
        return source
    result = dict(source)
    result["media"] = records
    return result


def _repair_media_parameters_from_driver_siblings(
    source: dict[str, Any], unified_root: Path
) -> dict[str, Any]:
    """Recover an all-zero media table from an intact same-driver sibling.

    A repair is accepted only when every candidate package using the exact
    ReferenceDriver driver ID agrees on the full four-field Paper.Param value for a
    material. Ambiguous driver families remain untouched for manual review.
    """

    media = list(source.get("media") or ())
    driver_id = str((source.get("driver") or {}).get("id") or "")
    if not driver_id or not media:
        return source
    parsed = [
        _legacy_four_fields(item.get("param"), record_name="Paper.Param")
        for item in media
    ]
    if any(parameters != (0, 0, 0, 0) for parameters in parsed):
        return source

    candidates: dict[str, set[tuple[int, int, int, int]]] = {}
    for printer_json in unified_root.glob("*/printer.json"):
        try:
            sibling = json.loads(printer_json.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if str((sibling.get("driver") or {}).get("id") or "") != driver_id:
            continue
        for item in sibling.get("media") or ():
            match = str(item.get("match") or "")
            if not match:
                continue
            parameters = _legacy_four_fields(
                item.get("param"), record_name="Paper.Param"
            )
            if parameters != (0, 0, 0, 0):
                candidates.setdefault(match, set()).add(parameters)

    repaired: list[dict[str, Any]] = []
    changed = False
    for raw_item in media:
        item = dict(raw_item)
        values = candidates.get(str(item.get("match") or ""), set())
        if len(values) == 1:
            parameters = next(iter(values))
            item["param"] = ",".join(str(value) for value in parameters)
            changed = True
        repaired.append(item)
    if not changed:
        return source
    result = dict(source)
    result["media"] = repaired
    return result


def _decoded_feed_parameters(value: object) -> dict[str, object]:
    """Decode sheet/roll mechanics and the exact REMOTE1 PP address."""

    feed_code, cut_code, packed_path, suppress_form_feed = _legacy_four_fields(
        value, record_name="Feed.Param"
    )
    try:
        medium_kind = _FEED_KINDS[feed_code]
        roll_cut_mode = _ROLL_CUT_MODES[cut_code]
    except KeyError as error:
        raise CatalogError(f"Unknown feed mechanics: {value!r}") from error
    if feed_code != 2 and cut_code != 0:
        raise CatalogError("A cut-sheet feed cannot request a roll cut mode")
    if suppress_form_feed not in {0, 1}:
        raise CatalogError("Feed form-feed suppression must be zero or one")
    return {
        "medium_kind": medium_kind,
        "roll_cut_mode": roll_cut_mode,
        "remote_paper_path": {
            "type": (packed_path >> 8) & 0xFF,
            "number": packed_path & 0xFF,
        },
        "suppress_form_feed_after_page": bool(suppress_form_feed),
    }


def _decoded_media_parameters(value: object) -> dict[str, object]:
    """Decode native media selection and physical raster-layout variant.

    The first value is the modern REMOTE1 media type. The second is the media
    class consumed by older ESC/P2 initialisation. The fourth value selects a
    model-specific physical head/raster layout; it is retained as a named
    selector and is never interpreted as a color curve.

    Первое поле — современный тип носителя, второе — код старой инициализации.
    Четвёртое выбирает физическую раскладку растра/головки конкретной модели,
    а не цветовой профиль или кривую.
    """

    remote_type, legacy_type, special_selector, layout_variant = _legacy_four_fields(
        value, record_name="Paper.Param"
    )
    if min(remote_type, legacy_type, layout_variant) < 0:
        raise CatalogError("Paper parameters must be non-negative")
    return {
        "remote_media_type_code": remote_type,
        "legacy_media_type_code": legacy_type,
        "special_media": _special_media(special_selector, record_name="Paper.Param"),
        "physical_raster_layout_variant": layout_variant,
    }


def _normalized_mechanical_parameters(source: dict[str, Any]) -> dict[str, Any]:
    """Replace every known positional Epson record with named open data."""

    source = _repair_known_media_parameters(source)
    result = dict(source)
    driver = dict(result.get("driver") or {})
    if "param" in driver:
        driver["driver_mechanics"] = _decoded_driver_parameters(driver.pop("param"))
        result["driver"] = driver

    groups = (
        ("media_sizes", "page_mechanics", _decoded_media_size_parameters),
        ("feeds", "feed_mechanics", _decoded_feed_parameters),
        ("media", "media_mechanics", _decoded_media_parameters),
    )
    for collection_name, target_name, decoder in groups:
        if collection_name not in result:
            continue
        records: list[dict[str, Any]] = []
        for raw_item in result.get(collection_name) or ():
            item = dict(raw_item)
            if collection_name == "media":
                searchable_name = f"{item.get('name', '')} {item.get('match', '')}".casefold()
                if any(
                    marker in searchable_name
                    for marker in _NONSTANDARD_BRANDED_MEDIA_MARKERS
                ):
                    # Branded aliases describe color-profile targets, not a
                    # distinct monochrome printer mechanism. Their source idx
                    # is intentionally not renumbered after removal.
                    # Брендированные ICC-носители не добавляют отдельной
                    # механики ЧБ-печати и не входят в продуктовый package.
                    continue
            if "param" in item:
                item[target_name] = decoder(item.pop("param"))
            if collection_name == "media":
                item = {
                    key: item[key]
                    for key in ("idx", "name", "match", target_name)
                    if key in item
                }
            records.append(item)
        result[collection_name] = records
    return result


def _normalize_inline_resolutions(source: dict[str, Any]) -> dict[str, Any]:
    """Decode resolution records when no richer runtime sidecar exists."""

    if "resolutions" not in source:
        return source
    result = dict(source)
    normalized: list[dict[str, object]] = []
    for index, item in enumerate(source.get("resolutions") or ()):
        record = dict(item)
        raw_parameters = record.pop("param", None)
        raw_weave = record.pop("weave", None)
        record.pop("curves", None)
        if raw_parameters is not None:
            record["escp2"] = _decoded_mode_parameters(raw_parameters)
        if raw_weave is not None:
            record["explicit_weave_sequence"] = _decoded_weave(raw_weave)
        if "app_dpi" in record:
            record["app_raster_dpi"] = _dpi_object(
                record.pop("app_dpi"), field=f"Resolution[{index}].ApplicationDPI"
            )
        if "printer_dpi" in record:
            record["printer_coordinate_dpi"] = _dpi_object(
                record.pop("printer_dpi"), field=f"Resolution[{index}].PrinterDPI"
            )
        if "quality" in record:
            record["quality_rank"] = int(record.pop("quality"))
        normalized.append(record)
    result["resolutions"] = normalized
    return result


def _normalized_descriptor(
    source: dict[str, Any], *, modes_are_in_runtime: bool = False
) -> bytes:
    """Keep runtime capabilities only; provenance and color curves stay outside."""

    source = _normalize_inline_resolutions(_normalized_mechanical_parameters(source))
    identity = source.get("identity") or {}
    normalized_identity = {
        key: identity[key]
        for key in (
            "name",
            "manufacturer",
            "marketing",
            "gutenprint_driver",
            "gutenprint_model_id",
        )
        if identity.get(key) is not None
    }
    normalized = {
        key: source[key]
        for key in (
            "schema",
            "protocol",
            "driver",
            "channels",
            "media_sizes",
            "feeds",
            "resolutions",
            "paper_types",
            "media",
            "device_features",
            "unimplemented_device_features",
        )
        if key in source
    }
    if modes_are_in_runtime:
        # Direct ESC/P2 descriptors historically duplicated their modes here
        # as anonymous integer arrays. The typed runtime descriptor is now the
        # single source of truth, so the opaque duplicate must not enter an
        # installed open-source package.
        #
        # Для прямого ESC/P2 typed runtime является единственным описанием
        # режимов. Сырой дубликат ``param=[...]`` в printer.json не сохраняем.
        normalized.pop("resolutions", None)
    normalized["identity"] = normalized_identity
    return _json_bytes(
        _without_paper_color_conversion(_without_color_assets(normalized))
    )


def _normalize_device_features(source: dict[str, Any]) -> dict[str, Any]:
    """Replace opaque positional Setting choices with named capabilities.

    Implemented feature choices and native command layouts live once in
    ``catalog_models.py``. A model package only declares which named features
    it supports. This removes repeated value tables from hundreds of packages
    and makes the installed data independent of a closed descriptor format.

    В package остаются только понятные имена возможностей. Таблицы вариантов и
    кодирование native-команд определены один раз в открытом коде backend.
    """

    implemented = {
        str(item) for item in source.get("device_features") or () if str(item)
    }
    unresolved: dict[str, dict[str, str]] = {
        str(item.get("identity")): dict(item)
        for item in source.get("unimplemented_device_features") or ()
        if isinstance(item, dict) and item.get("identity")
    }
    for setting in source.get("settings") or ():
        raw_choices = list(setting.get("choices") or ())
        if not raw_choices:
            continue
        parsed: list[tuple[int, int, int, int, str]] = []
        for raw_choice in raw_choices:
            fields = str(raw_choice).split(",", 4)
            if len(fields) != 5:
                raise CatalogError(
                    f"Malformed device setting choice: {raw_choice!r}"
                )
            try:
                setting_type, value, reserved_1, reserved_2 = (
                    int(fields[index]) for index in range(4)
                )
            except ValueError as error:
                raise CatalogError(
                    f"Non-integer device setting fields: {raw_choice!r}"
                ) from error
            if reserved_1 != 0 or reserved_2 != 0:
                raise CatalogError(
                    "Unknown non-zero reserved device-setting fields: "
                    f"{raw_choice!r}"
                )
            parsed.append((setting_type, value, reserved_1, reserved_2, fields[4]))
        setting_types = {item[0] for item in parsed}
        if len(setting_types) != 1:
            raise CatalogError("One device setting mixes multiple setting types")
        setting_type = next(iter(setting_types))
        definition = _DESCRIPTOR_SETTING_TYPES.get(setting_type)
        if definition is None:
            raise CatalogError(f"Unknown device setting type: {setting_type}")
        identity, default_name, is_implemented = definition
        display_name = str(setting.get("name") or default_name)
        if is_implemented:
            implemented.add(identity)
        else:
            unresolved[identity] = {
                "identity": identity,
                "display_name": display_name,
                "status": "NATIVE_ENCODING_NOT_IMPLEMENTED",
            }
    result = dict(source)
    result.pop("settings", None)
    result["device_features"] = sorted(implemented)
    if unresolved:
        result["unimplemented_device_features"] = [
            unresolved[key] for key in sorted(unresolved)
        ]
    else:
        result.pop("unimplemented_device_features", None)
    return result


def _descriptor_device_settings(
    source: dict[str, Any], descriptor_root: Path | None
) -> dict[str, Any]:
    """Merge every ``[Setting*]`` section from an optional Epson descriptor.

    Some older unified exports retained only the last settings section because
    their section-name key was not unique. The catalog importer accepts the
    original plain-text descriptor as an optional repair input, while the
    installed package remains fully self-contained.

    Старые выгрузки могли оставить только последнюю секцию настроек. При
    наличии исходного текстового описателя импортёр восстанавливает все секции
    до создания автономного package.
    """

    if descriptor_root is None:
        return source
    model_name = str((source.get("identity") or {}).get("name") or "").strip()
    if not model_name:
        return source
    descriptor = descriptor_root / f"{model_name}.tpp"
    if not descriptor.is_file():
        return source

    settings: list[dict[str, object]] = []
    current_name: str | None = None
    current_values: dict[str, str] = {}

    def finish_section() -> None:
        nonlocal current_name, current_values
        if current_name is not None and current_name.casefold().startswith("setting"):
            choices = [
                value
                for key, value in sorted(
                    current_values.items(),
                    key=lambda item: (
                        int(re.search(r"\d+", item[0]).group())
                        if re.search(r"\d+", item[0])
                        else 0
                    ),
                )
                if key.casefold().startswith("choice")
            ]
            if choices:
                settings.append(
                    {
                        "section": current_name,
                        "name": current_values.get("Name", current_name),
                        "choices": choices,
                    }
                )
        current_name = None
        current_values = {}

    for raw_line in descriptor.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith(";"):
            continue
        section = re.fullmatch(r"\[([^]]+)](?:\s*;.*)?", line)
        if section:
            finish_section()
            current_name = section.group(1).strip()
            continue
        if current_name is not None and "=" in line:
            key, value = line.split("=", 1)
            current_values[key.strip()] = value.split(";", 1)[0].strip()
    finish_section()
    if not settings:
        return source
    result = dict(source)
    result["settings"] = settings
    return result


def _dpi_object(value: object, *, field: str) -> dict[str, int]:
    numbers = tuple(int(item) for item in re.findall(r"\d+", str(value or "")))
    if len(numbers) < 2 or min(numbers[:2]) <= 0:
        raise CatalogError(f"Invalid {field}: {value!r}")
    return {"horizontal": numbers[0], "vertical": numbers[1]}


def _mode_scope(value: object) -> dict[str, object]:
    """Decode ``Mode<cartridge>_<paper>_<variant>`` section membership."""

    match = re.fullmatch(r"(?P<cartridge>\d+|X)_(?P<paper>\d+|X)", str(value or ""))
    if match is None:
        raise CatalogError(f"Invalid mode cartridge/paper selector: {value!r}")
    paper = match.group("paper")
    cartridge = match.group("cartridge")
    return {
        "cartridge_scope": "all" if cartridge == "X" else "one_cartridge",
        **({} if cartridge == "X" else {"cartridge_index": int(cartridge)}),
        "media_scope": "all_non_default_media" if paper == "X" else "one_media",
        **({} if paper == "X" else {"media_index": int(paper)}),
    }


def _decoded_weave(value: object) -> list[dict[str, int]]:
    """Decode the optional four-character Epson software-weave sequence.

    Every entry stores ``feed, nozzle phase, pass within phase``. The two feed
    characters use ``10 * (first - '0') + (second - '0')``; punctuation and
    letters intentionally extend that printable numeric range. For example,
    ``>3D0`` becomes feed 143, nozzle phase 3, pass 0. This is the exact parser
    used by the descriptor engine, expressed as named data instead of an
    unexplained string.

    Каждые четыре символа задают подачу, фазу сопел и проход внутри фазы.
    Результат хранится именованными числами, а не строкой ``>3D0``.
    """

    encoded = str(value or "")
    if not encoded:
        return []
    if len(encoded) % 4:
        raise CatalogError(f"Software-weave string is not a multiple of four: {encoded!r}")
    result: list[dict[str, int]] = []
    for offset in range(0, len(encoded), 4):
        entry = encoded[offset : offset + 4]
        nozzle_phase = ord(entry[2]) - ord("A")
        pass_in_phase = ord(entry[3]) - ord("0")
        if nozzle_phase < 0 or pass_in_phase < 0:
            raise CatalogError(f"Invalid software-weave entry: {entry!r}")
        result.append(
            {
                "relative_feed_units": 10 * (ord(entry[0]) - ord("0"))
                + ord(entry[1])
                - ord("0"),
                "nozzle_phase": nozzle_phase,
                "pass_in_phase": pass_in_phase,
            }
        )
    return result


def _decoded_mode_parameters(value: object) -> dict[str, object]:
    """Name every field of an Epson direct-raster mode parameter record.

    The five positions were established by following the descriptor fields
    into the Epson stream builder and checking generated streams:

    0. Variable-size-dot code, emitted by ``ESC ( e``.
    1. Packed software-weave layout. For values >= 16 the high nibble is the
       pass-group count and the low nibble selects the weave variant.
    2. Mode flags: bit 0 chooses the default ESC U direction, bit 2 emits the
       legacy ``ESC ( s 02`` command. Bits 1/3 are retained as explicit
       internal flags; they do not directly emit a command in the Epson path.
    3. Default ``ESC ( m`` print-method byte.
    4. Optional alternative ``ESC ( m`` byte selected by the model's
       microweave-precision option.

    Это граница импорта: позиционный массив разбирается один раз и никогда не
    попадает в установленный каталог.
    """

    parameters = tuple(int(item) for item in value or ())
    if len(parameters) not in {4, 5}:
        raise CatalogError(
            f"Epson mode parameters must contain four or five integers: {value!r}"
        )
    dot_mode, packed_weave, flags, default_method = parameters[:4]
    if not all(0 <= item <= 0xFF for item in parameters):
        raise CatalogError(f"Epson mode parameter does not fit one byte: {value!r}")
    if packed_weave < 0x10:
        weave_layout: dict[str, object] = {
            "encoding": "legacy_direct",
            "legacy_layout_code": packed_weave,
            "pass_group_count": 1,
        }
    else:
        weave_layout = {
            "encoding": "packed_nibbles",
            "pass_group_count": packed_weave >> 4,
            "variant_code": packed_weave & 0x0F,
        }
    return {
        "variable_dot_mode": dot_mode,
        "software_weave": weave_layout,
        "direction_and_layout": {
            "default_direction": "bidirectional" if flags & 0x01 else "unidirectional",
            "internal_layout_flag_1": bool(flags & 0x02),
            "emit_legacy_esc_s_mode_2": bool(flags & 0x04),
            "internal_layout_flag_3": bool(flags & 0x08),
            "unknown_high_bits": flags & 0xF0,
        },
        "print_method": {
            "default": default_method,
            **({"precision_variant": parameters[4]} if len(parameters) == 5 else {}),
        },
    }


def _normalized_mode_runtime(
    path: Path | None, source_resolutions: object = None
) -> bytes | None:
    """Convert source mode records into the documented pyToneRIP schema."""

    if path is None:
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("schema") == "pytone-rip.mode-runtime-v2":
        modes = list(value.get("modes") or ())
    else:
        modes = []
        for item in value.get("modes") or ():
            scope = _mode_scope(item.get("_cart_paper"))
            variant_index = int(item.get("_q", 0))
            coordinates = _dpi_object(item.get("printer_dpi"), field="PrinterDPI")
            description = str(item.get("description") or "").strip()
            nominal_match = re.fullmatch(
                r"(\d+)(?:\s*[x×]\s*(\d+))?\s*dpi", description, re.I
            )
            nominal_resolution = (
                {
                    "horizontal": int(nominal_match.group(1)),
                    "vertical": int(nominal_match.group(2) or coordinates["vertical"]),
                }
                if nominal_match else None
            )
            modes.append(
                {
                    "display_name": str(item.get("name") or f"Mode {variant_index + 1}"),
                    "quality_rank": int(item.get("quality") or 0),
                    "variant_index": variant_index,
                    "app_raster_dpi": _dpi_object(item.get("app_dpi"), field="ApplicationDPI"),
                    "printer_coordinate_dpi": coordinates,
                    **({"nominal_output_dpi": nominal_resolution} if nominal_resolution else {}),
                    **(
                        {
                            "drop_levels_percent": [int(v) for v in item["drop_levels_percent"]],
                            "raster_bit_depth": int(item.get("raster_bit_depth") or 2),
                        }
                        if item.get("drop_levels_percent") else {}
                    ),
                    "applicability": scope,
                    "escp2": _decoded_mode_parameters(item.get("param")),
                    "explicit_weave_sequence": _decoded_weave(item.get("weave")),
                }
            )

    # Older raw sidecars and already-normalized v2 sidecars can omit metadata
    # retained in the unified descriptor. Join only exact mechanical modes;
    # different curves with the same command key are deliberately ambiguous.
    def key(item: dict[str, Any], *, normalized: bool) -> tuple[object, ...]:
        if normalized:
            name = item.get("display_name")
            quality = item.get("quality_rank")
            app = item.get("app_raster_dpi")
            printer = item.get("printer_coordinate_dpi")
            parameters = item.get("escp2")
            weave = item.get("explicit_weave_sequence")
        else:
            name = item.get("name")
            quality = item.get("quality")
            app = _dpi_object(item.get("app_dpi"), field="ApplicationDPI")
            printer = _dpi_object(item.get("printer_dpi"), field="PrinterDPI")
            parameters = _decoded_mode_parameters(item.get("param"))
            weave = _decoded_weave(item.get("weave"))
        return (
            str(name), int(quality or 0),
            int(app["horizontal"]), int(app["vertical"]),
            int(printer["horizontal"]), int(printer["vertical"]),
            json.dumps(parameters, sort_keys=True),
            json.dumps(weave, sort_keys=True),
        )

    metadata: dict[tuple[object, ...], set[str]] = {}
    for source_mode in source_resolutions or ():
        if not isinstance(source_mode, dict):
            continue
        try:
            mode_key = key(source_mode, normalized=False)
        except (CatalogError, KeyError, TypeError, ValueError):
            continue
        data = {
            field: source_mode[field]
            for field in ("description", "drop_levels_percent", "raster_bit_depth")
            if field in source_mode
        }
        metadata.setdefault(mode_key, set()).add(json.dumps(data, sort_keys=True))
    for mode in modes:
        candidates = metadata.get(key(mode, normalized=True), set())
        if len(candidates) != 1:
            continue
        source_data = json.loads(next(iter(candidates)))
        description = str(source_data.get("description") or "").strip()
        coordinate_y = int(mode["printer_coordinate_dpi"]["vertical"])
        nominal_match = re.fullmatch(
            r"(\d+)(?:\s*[x×]\s*(\d+))?\s*dpi", description, re.I
        )
        if nominal_match and "nominal_output_dpi" not in mode:
            mode["nominal_output_dpi"] = {
                "horizontal": int(nominal_match.group(1)),
                "vertical": int(nominal_match.group(2) or coordinate_y),
            }
        if source_data.get("drop_levels_percent") and "drop_levels_percent" not in mode:
            mode["drop_levels_percent"] = source_data["drop_levels_percent"]
            mode["raster_bit_depth"] = int(source_data.get("raster_bit_depth") or 2)
    return _json_bytes({"schema": "pytone-rip.mode-runtime-v2", "modes": modes})


def _choose_ppd(
    source_dir: Path, *, expected_model_id: str | None = None,
    verified_family_mapping: bool = False,
) -> tuple[Path, str]:
    vendor = source_dir / "vendor.ppd"
    generated = source_dir / "printer.ppd"
    vendor_matches = (
        vendor.is_file()
        and _is_official_epson_ppd(vendor)
        and (
            expected_model_id is None
            or verified_family_mapping
            or _ppd_matches_model(vendor, expected_model_id)
        )
    )
    selected = vendor if vendor_matches else generated
    if not selected.is_file():
        raise CatalogError(f"Unified package has no vendor.ppd or printer.ppd: {source_dir.name}")
    # Parsing now prevents a broken PPD from entering the persistent catalog.
    read_ppd(selected)
    return selected, "vendor.ppd" if selected == vendor else "printer.ppd"


def _is_official_epson_ppd(path: Path) -> bool:
    """A vendor filename alone does not establish a PPD's provenance."""

    ppd = read_ppd(path)
    manufacturer = ppd.metadata.get("Manufacturer", "").casefold()
    nickname = ppd.metadata.get("NickName", "").casefold()
    return "epson" in manufacturer and not any(
        marker in nickname for marker in ("pytone", "pyrip")
    )


def _ppd_matches_model(path: Path, model_id: str) -> bool:
    """Reject a different numbered model unless an explicit family map exists."""

    model_name = read_ppd(path).metadata.get("ModelName", "")
    expected_numbers = set(re.findall(r"\d+", model_id))
    ppd_numbers = set(re.findall(r"\d+", model_name))
    return not expected_numbers or bool(expected_numbers & ppd_numbers)


def _normalized_ppd(path: Path) -> bytes:
    """Remove generator provenance comments while retaining every PPD directive."""

    text = path.read_text(encoding="latin-1")
    lines = [line for line in text.splitlines() if not line.lstrip().startswith("*%")]
    return ("\n".join(lines) + "\n").encode("latin-1")


def _ppd_identifier(value: object, fallback: str) -> str:
    token = re.sub(r"[^A-Za-z0-9]+", "_", str(value or "")).strip("_")
    return token or fallback


def _ppd_label(value: object) -> str:
    # PPD strings are quoted; keep generated labels single-line and escape the
    # two characters that have syntax inside a quoted PostScript string.
    return str(value or "").replace("\\", "\\\\").replace('"', '\\"').replace("\r", " ").replace("\n", " ")


def _generated_catalog_ppd(
    source: dict[str, Any],
    original: bytes,
    backend_runtime: bytes | None,
) -> bytes:
    """Generate an internal PPD from the final merged printer mechanics.

    A fallback PPD from a source package may contain only a small UI subset.
    Reusing it hid valid high-resolution modes even though model XML contained
    the complete table. Vendor PPDs are never passed here and remain byte-for-
    byte authoritative. / Fallback PPD строится заново из итоговой механики;
    оригинальный vendor PPD этим кодом никогда не заменяется.
    """

    original_ppd = read_ppd_bytes(original)
    runtime = json.loads(backend_runtime.decode("utf-8")) if backend_runtime else {}
    modes = list(runtime.get("modes") or source.get("resolutions") or ())
    media = list(
        runtime.get("paper_types")
        if runtime.get("schema") == "pytone-rip.gutenprint-runtime"
        else source.get("media") or source.get("paper_types") or ()
    )
    feeds = list(source.get("feeds") or runtime.get("feeds") or ())
    identity = source.get("identity") or {}
    captured_media = CAPTURED_PRO_MEDIA_BY_DRIVER.get(
        str(identity.get("gutenprint_driver") or "")
    )
    if captured_media:
        media = [{"name": item.display_name} for item in captured_media]
    model_name = _ppd_label(
        identity.get("marketing") or identity.get("name") or "Epson printer"
    )
    lines = [
        '*PPD-Adobe: "4.3"',
        '*FormatVersion: "4.3"',
        '*FileVersion: "1.0"',
        '*LanguageVersion: English',
        '*LanguageEncoding: UTF-8',
        '*PCFileName: "PYTONERI.PPD"',
        '*Manufacturer: "Epson"',
        f'*ModelName: "{model_name}"',
        f'*NickName: "{model_name} pyToneRIP"',
        '*ColorDevice: True',
    ]
    pages = list(original_ppd.pages)
    if not pages:
        raise CatalogError("Fallback PPD generation requires at least one page size")
    default_page = next((item.name for item in pages if item.name == "A4"), pages[0].name)
    lines.extend(
        [
            '*OpenUI *PageSize/Media Size: PickOne',
            f'*DefaultPageSize: {default_page}',
        ]
    )
    for page in pages:
        label = _ppd_label(page.label)
        lines.append(
            f'*PageSize {page.name}/{label}: "<< /PageSize [{page.width_points:g} {page.height_points:g}] >> setpagedevice"'
        )
    lines.append('*CloseUI: *PageSize')
    for page in pages:
        lines.append(f'*PaperDimension {page.name}: "{page.width_points:g} {page.height_points:g}"')
        lines.append(
            f'*ImageableArea {page.name}: "{page.imageable_left_points:g} {page.imageable_bottom_points:g} {page.imageable_right_points:g} {page.imageable_top_points:g}"'
        )

    if media:
        media_choices: list[tuple[str, str]] = []
        used_media: set[str] = set()
        for index, item in enumerate(media):
            label = str(item.get("text") or item.get("name") or f"Media {index + 1}")
            name = _ppd_identifier(label, f"Media{index + 1}")
            if name in used_media:
                continue
            used_media.add(name)
            media_choices.append((name, _ppd_label(label)))
        lines.extend(
            [
                '*OpenUI *MediaType/Media Type: PickOne',
                f'*DefaultMediaType: {media_choices[0][0]}',
            ]
        )
        for name, label in media_choices:
            lines.append(f'*MediaType {name}/{label}: "<< /MediaType ({label}) >> setpagedevice"')
        lines.append('*CloseUI: *MediaType')

    if modes:
        resolution_choices: list[tuple[str, str, int, int]] = []
        used_modes: set[str] = set()
        for index, item in enumerate(modes):
            name = _ppd_identifier(
                item.get("identity") or item.get("name"), f"Mode{index + 1}"
            )
            if name in used_modes:
                continue
            used_modes.add(name)
            x = int(item.get("horizontal_dpi") or 0)
            y = int(item.get("vertical_dpi") or 0)
            if x <= 0 or y <= 0:
                values = tuple(
                    int(value)
                    for value in re.findall(
                        r"\d+", str(item.get("printer_dpi") or item.get("app_dpi") or "")
                    )
                )
                if len(values) >= 2:
                    x, y = values[:2]
            if x <= 0 or y <= 0:
                continue
            label = _ppd_label(item.get("display_name") or item.get("name") or f"{x} x {y} DPI")
            resolution_choices.append((name, label, x, y))
        if resolution_choices:
            default_resolution = next(
                (item[0] for item in resolution_choices if item[2:] == (720, 720)),
                resolution_choices[0][0],
            )
            lines.extend(
                [
                    '*OpenUI *Resolution/Print Resolution: PickOne',
                    f'*DefaultResolution: {default_resolution}',
                ]
            )
            for name, label, x, y in resolution_choices:
                lines.append(
                    f'*Resolution {name}/{label}: "<< /HWResolution [{x} {y}] >> setpagedevice"'
                )
            lines.append('*CloseUI: *Resolution')

    if feeds:
        feed_choices: list[tuple[str, str]] = []
        used_feeds: set[str] = set()
        for index, item in enumerate(feeds):
            label = str(item.get("text") or item.get("name") or f"Feed {index + 1}")
            name = _ppd_identifier(item.get("name") or label, f"Feed{index + 1}")
            if name in used_feeds:
                continue
            used_feeds.add(name)
            feed_choices.append((name, _ppd_label(label)))
        lines.extend(
            [
                '*OpenUI *InputSlot/Paper Source: PickOne',
                f'*DefaultInputSlot: {feed_choices[0][0]}',
            ]
        )
        for name, label in feed_choices:
            lines.append(f'*InputSlot {name}/{label}: "<< /MediaPosition 0 >> setpagedevice"')
        lines.append('*CloseUI: *InputSlot')
    lines.append('*cupsFilter: "application/vnd.cups-raster 0 -"')
    return ("\n".join(lines) + "\n").encode("utf-8")


def read_ppd_bytes(value: bytes):
    """Parse generated-input bytes with the same encoding policy as read_ppd."""

    from xp750_rip.capabilities.ppd import parse_ppd

    try:
        text = value.decode("utf-8")
    except UnicodeDecodeError:
        text = value.decode("latin-1")
    return parse_ppd(text)


def _runtime_source(unified_root: Path, package_id: str, source: dict[str, Any]) -> Path | None:
    """Find source-only mechanics still omitted from the unified schema.

    Unified v1.2 now carries vendor ``Mode.Param``, ``Weave`` and device
    settings. It still deduplicates equivalent resolution records and omits the
    private ``_cart_paper`` association. The copied PF sidecar is therefore used
    only to retain media-to-mode membership; its color curves are never loaded.

    GP needs model XML head geometry. Both sidecars are copied into the support
    package, so an installed RIP never depends on the research workspace.
    """

    identity = source.get("identity") or {}
    if package_id.startswith("pf--"):
        model = package_id.removeprefix("pf--")
        candidate = unified_root.parent / "exported" / model / "printer.json"
    else:
        driver = str(identity.get("gutenprint_driver") or package_id.removeprefix("gp--"))
        candidate = unified_root.parent / "exported_gutenprint" / driver / "printer.json"
    return candidate if candidate.is_file() else None


def _resolved_gutenprint_runtime(
    source: dict[str, Any], *, gutenprint_root: Path
) -> bytes | None:
    """Build one self-contained runtime record from the complete XML graph.

    Gutenprint model records frequently inherit heads, inks, resolutions and
    paper tables through ``base``/``src``. Resolving the graph here prevents a
    packaged RIP from depending on a developer Gutenprint installation.

    Наследование XML разрешается во время импорта. В установленной программе
    остаются только нормализованные данные конкретной модели.
    """

    identity = source.get("identity") or {}
    driver_id = str(identity.get("gutenprint_driver") or "").strip()
    model_id = str(identity.get("gutenprint_model_id") or "").strip()
    if not driver_id or not model_id:
        return None
    selection = GutenprintSelection(
        name=str(identity.get("name") or driver_id),
        driver_id=driver_id,
        model_id=model_id,
        parameters=None,
        source="Gutenprint printer registry",
        override_used=False,
    )
    dependencies = GutenprintResolver(gutenprint_root).dependencies(selection)
    if dependencies.unresolved:
        raise CatalogError(
            "Unresolved Gutenprint XML dependencies for "
            f"{driver_id}: {', '.join(dependencies.unresolved)}"
        )
    runtime = resolved_gutenprint_catalog_data(dependencies)
    return _json_bytes(
        _without_paper_color_conversion(_without_color_assets(runtime))
    )


def _backend_sources(
    unified_root: Path,
    package_id: str,
    source: dict[str, Any],
    *,
    gutenprint_root: Path,
) -> tuple[Path | None, bytes | None]:
    driver_id = str((source.get("driver") or {}).get("id") or "")
    explicit_id = _BACKEND_DATA_PACKAGES.get(package_id)
    supplemental_id = explicit_id or _BACKEND_DATA_DRIVERS.get(driver_id)
    if supplemental_id is None:
        return None, None
    supplemental = unified_root / supplemental_id / "printer.json"
    if not supplemental.is_file():
        if explicit_id is None:
            # Driver-family enrichment is optional for a minimal/custom
            # unified source. The original ReferenceDriver runtime remains usable;
            # production imports include the matched Gutenprint package.
            return None, None
        raise CatalogError(
            f"Explicit backend data package is missing for {package_id}: {supplemental_id}"
        )
    value = json.loads(supplemental.read_text(encoding="utf-8"))
    resolved = _resolved_gutenprint_runtime(value, gutenprint_root=gutenprint_root)
    if resolved is not None:
        return supplemental, resolved
    runtime = _runtime_source(unified_root, supplemental_id, value)
    return supplemental, _normalized_mode_runtime(runtime)


def import_unified_package(
    package_id: str,
    *,
    unified_root: Path = DEFAULT_UNIFIED_ROOT,
    catalog_root: Path = DEVELOPMENT_CATALOG_ROOT,
    gutenprint_root: Path = GUTENPRINT_ROOT,
    device_settings_root: Path | None = None,
    visible: bool = True,
    force: bool = False,
) -> CatalogPackage:
    """Import exactly one Epson package using vendor PPD priority."""

    package_id = _safe_package_id(package_id)
    source_dir = (unified_root.resolve() / package_id).resolve()
    if source_dir.parent != unified_root.resolve() or not source_dir.is_dir():
        raise FileNotFoundError(f"Unified package not found: {package_id}")
    printer_json = source_dir / "printer.json"
    if not printer_json.is_file():
        raise CatalogError(f"Unified package has no printer.json: {package_id}")
    target = _package_directory(catalog_root, package_id)
    source = json.loads(printer_json.read_text(encoding="utf-8"))
    identity = source.get("identity") or {}
    if str(identity.get("manufacturer") or "").casefold() != "epson":
        raise CatalogError(f"Only Epson packages are accepted: {package_id}")
    if source.get("schema") not in {
        "rip-printer-v1",
        "rip-printer-v1.1",
        "rip-printer-v1.2",
    }:
        raise CatalogError(f"Unsupported unified schema in {package_id}: {source.get('schema')}")
    source = _repair_media_parameters_from_driver_siblings(source, unified_root)
    source = _correct_direct_channels(
        _normalize_device_features(
            _descriptor_device_settings(source, device_settings_root)
        ),
        package_id,
    )

    selected_ppd, ppd_source = _choose_ppd(
        source_dir,
        expected_model_id=_normalized_model_id(source, package_id),
        verified_family_mapping=bool(identity.get("family_mapping_source")),
    )
    selected_ppd_data = _normalized_ppd(selected_ppd)
    runtime_source = _runtime_source(unified_root, package_id, source)
    runtime_source_data = _normalized_mode_runtime(runtime_source, source.get("resolutions"))
    existing_runtime = target / "runtime-source.json"
    if runtime_source_data is None and force and existing_runtime.is_file():
        # A refreshed knowledge bundle may already contain normalized modes and
        # omit its older sidecar. Keep the previously packaged media-to-mode
        # membership until the new descriptor proves it supersedes that data.
        # Неполный вход при force-обновлении не должен удалять уже сохранённую
        # механику модели.
        runtime_source_data = _normalized_mode_runtime(existing_runtime, source.get("resolutions"))
    normalized_source = _normalized_descriptor(
        source, modes_are_in_runtime=runtime_source_data is not None
    )
    backend_source, backend_runtime = _backend_sources(
        unified_root,
        package_id,
        source,
        gutenprint_root=gutenprint_root,
    )
    own_gutenprint_runtime = _resolved_gutenprint_runtime(
        source, gutenprint_root=gutenprint_root
    )
    if own_gutenprint_runtime is not None:
        backend_runtime = own_gutenprint_runtime
    if ppd_source != "vendor.ppd":
        selected_ppd_data = _generated_catalog_ppd(
            source, selected_ppd_data, backend_runtime
        )
        # Parse the generated file before any persistent catalog replacement.
        read_ppd_bytes(selected_ppd_data)
    backend_source_data = (
        _normalized_descriptor(
            json.loads(backend_source.read_text(encoding="utf-8")),
            modes_are_in_runtime=(
                backend_runtime is not None
                and json.loads(backend_runtime.decode("utf-8")).get("schema")
                == "pytone-rip.mode-runtime-v2"
            ),
        )
        if backend_source is not None
        else None
    )
    model_id = _normalized_model_id(source, package_id)
    protocol_class, family, backend_family, warnings = _correct_protocol(
        package_id,
        source,
        allow_driver_override=backend_runtime is not None,
    )
    if ppd_source != "vendor.ppd":
        driver_id = str((source.get("driver") or {}).get("id") or "").strip()
        technical_source = (
            f"ReferenceDriver driver {driver_id}" if driver_id
            else str((source.get("identity") or {}).get("gutenprint_driver") or "source model")
        )
        warnings = (*warnings, f"SYNTHETIC_PPD: no Epson manufacturer PPD supplied; technical source {technical_source}")
    manifest_path = target / "manifest.json"
    if target.exists() and not force:
        raise FileExistsError(f"Printer support package already exists: {package_id}")
    if manifest_path.is_file():
        visible = CatalogPackage.from_json(
            json.loads(manifest_path.read_text(encoding="utf-8"))
        ).visible

    manifest = {
        "schema": PACKAGE_SCHEMA,
        "version": PACKAGE_VERSION,
        "package_id": package_id,
        "model_id": model_id,
        "display_name": _display_name(source),
        "protocol_class": protocol_class,
        "protocol_family": family,
        "backend_family": backend_family,
        "backend_status": _backend_status(protocol_class),
        "visible": bool(visible),
        "source_sha256": _sha256(printer_json),
        "ppd_file": "vendor.ppd" if ppd_source == "vendor.ppd" else "printer.ppd",
        "ppd_source": ppd_source,
        "ppd_sha256": hashlib.sha256(selected_ppd_data).hexdigest(),
        "runtime_source_file": "runtime-source.json" if runtime_source_data else None,
        "runtime_source_sha256": (
            hashlib.sha256(runtime_source_data).hexdigest()
            if runtime_source_data
            else None
        ),
        "backend_source_file": "backend-source.json" if backend_source else None,
        "backend_source_sha256": _sha256(backend_source) if backend_source else None,
        "backend_runtime_file": "backend-runtime.json" if backend_runtime else None,
        "backend_runtime_sha256": (
            hashlib.sha256(backend_runtime).hexdigest() if backend_runtime else None
        ),
        "warnings": list(warnings),
    }

    # Stage a complete package beside the destination, then use directory
    # replacement. A failed copy cannot corrupt the last working package.
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=package_id + ".", dir=target.parent))
    try:
        (staging / "printer.json").write_bytes(normalized_source)
        (staging / manifest["ppd_file"]).write_bytes(selected_ppd_data)
        if runtime_source_data is not None:
            (staging / "runtime-source.json").write_bytes(runtime_source_data)
        if backend_source_data is not None:
            (staging / "backend-source.json").write_bytes(backend_source_data)
        if backend_runtime is not None:
            (staging / "backend-runtime.json").write_bytes(backend_runtime)
        (staging / "manifest.json").write_bytes(_json_bytes(manifest))
        backup = target.with_name(target.name + ".previous")
        if backup.exists():
            shutil.rmtree(backup)
        if target.exists():
            os.replace(target, backup)
        try:
            os.replace(staging, target)
        except Exception:
            if backup.exists():
                os.replace(backup, target)
            raise
        if backup.exists():
            shutil.rmtree(backup)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    rebuild_catalog_index(catalog_root)
    return CatalogPackage.from_json(manifest)


def import_family_alias(
    alias_package_id: str,
    *,
    base_package_id: str,
    model_id: str,
    display_name: str,
    vendor_ppd: Path,
    catalog_root: Path = DEVELOPMENT_CATALOG_ROOT,
    mapping_source: str,
    visible: bool = True,
    force: bool = False,
) -> CatalogPackage:
    """Create an exact model package from an explicitly mapped family driver.

    ReferenceDriver lists many marketing models as aliases of one technical driver.
    The base package remains the authority for channels, ESC/P2 commands,
    modes and media mechanics, while the model's own Epson PPD is the
    authority for page sizes and UI capabilities.  No name or number based
    family inference is performed here.
    """

    alias_package_id = _safe_package_id(alias_package_id)
    base_package_id = _safe_package_id(base_package_id)
    model_id = _safe_model_id(model_id)
    if alias_package_id == base_package_id:
        raise CatalogError("A family alias must differ from its base package")
    if not vendor_ppd.is_file():
        raise FileNotFoundError(f"Exact vendor PPD not found: {vendor_ppd}")
    if not _is_official_epson_ppd(vendor_ppd):
        raise CatalogError(f"Family alias requires an Epson manufacturer PPD: {vendor_ppd}")

    base_dir = _package_directory(catalog_root, base_package_id)
    base_manifest_path = base_dir / "manifest.json"
    if not base_manifest_path.is_file():
        raise CatalogError(f"Mapped base package is not imported: {base_package_id}")
    base_manifest = CatalogPackage.from_json(
        json.loads(base_manifest_path.read_text(encoding="utf-8"))
    )
    if base_manifest.backend_status != "IMPLEMENTED":
        raise CatalogError(
            f"Mapped base package has no implemented backend: {base_package_id}"
        )

    # Validate and normalize the exact PPD before touching the catalog.
    read_ppd(vendor_ppd)
    ppd_data = _normalized_ppd(vendor_ppd)
    read_ppd_bytes(ppd_data)

    source_path = base_dir / "printer.json"
    source = json.loads(source_path.read_text(encoding="utf-8"))
    source["identity"] = {
        **(source.get("identity") or {}),
        "manufacturer": "Epson",
        "name": "Epson_" + model_id,
        "marketing": display_name.removeprefix("Epson "),
        "family_driver": base_package_id,
        "family_mapping_source": mapping_source,
    }
    source_data = _json_bytes(source)
    target = _package_directory(catalog_root, alias_package_id)
    manifest_path = target / "manifest.json"
    if target.exists() and not force:
        raise FileExistsError(
            f"Printer support package already exists: {alias_package_id}"
        )
    if manifest_path.is_file():
        visible = CatalogPackage.from_json(
            json.loads(manifest_path.read_text(encoding="utf-8"))
        ).visible

    copied_files = {
        "runtime_source_file": base_manifest.runtime_source_file,
        "backend_source_file": base_manifest.backend_source_file,
        "backend_runtime_file": base_manifest.backend_runtime_file,
    }
    warning = (
        f"FAMILY_ALIAS: {mapping_source}; technical driver {base_package_id}; "
        f"exact capabilities from {vendor_ppd.name}"
    )
    manifest = {
        "schema": PACKAGE_SCHEMA,
        "version": PACKAGE_VERSION,
        "package_id": alias_package_id,
        "model_id": model_id,
        "display_name": display_name,
        "protocol_class": base_manifest.protocol_class,
        "protocol_family": base_manifest.protocol_family,
        "backend_family": base_manifest.backend_family,
        "backend_status": base_manifest.backend_status,
        "visible": bool(visible),
        "source_sha256": hashlib.sha256(source_data).hexdigest(),
        "ppd_file": "vendor.ppd",
        "ppd_source": "vendor.ppd",
        "ppd_sha256": hashlib.sha256(ppd_data).hexdigest(),
        "runtime_source_file": base_manifest.runtime_source_file,
        "runtime_source_sha256": None,
        "backend_source_file": base_manifest.backend_source_file,
        "backend_source_sha256": None,
        "backend_runtime_file": base_manifest.backend_runtime_file,
        "backend_runtime_sha256": None,
        "warnings": [*base_manifest.warnings, warning],
    }

    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=alias_package_id + ".", dir=target.parent))
    try:
        (staging / "printer.json").write_bytes(source_data)
        (staging / "vendor.ppd").write_bytes(ppd_data)
        for field, filename in copied_files.items():
            if filename is None:
                continue
            source_file = base_dir / filename
            if not source_file.is_file():
                raise CatalogError(
                    f"Mapped base package is missing {field}: {source_file}"
                )
            data = source_file.read_bytes()
            (staging / filename).write_bytes(data)
            manifest[field.replace("_file", "_sha256")] = hashlib.sha256(data).hexdigest()
        (staging / "manifest.json").write_bytes(_json_bytes(manifest))
        backup = target.with_name(target.name + ".previous")
        if backup.exists():
            shutil.rmtree(backup)
        if target.exists():
            os.replace(target, backup)
        try:
            os.replace(staging, target)
        except Exception:
            if backup.exists():
                os.replace(backup, target)
            raise
        if backup.exists():
            shutil.rmtree(backup)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    rebuild_catalog_index(catalog_root)
    return CatalogPackage.from_json(manifest)


def load_catalog_packages(root: Path = CATALOG_ROOT) -> tuple[CatalogPackage, ...]:
    packages_dir = root / "packages"
    if not packages_dir.is_dir():
        return ()
    result: list[CatalogPackage] = []
    for path in sorted(packages_dir.glob("*/manifest.json")):
        result.append(CatalogPackage.from_json(json.loads(path.read_text(encoding="utf-8"))))
    model_ids = [item.model_id.casefold() for item in result]
    if len(model_ids) != len(set(model_ids)):
        raise CatalogError("Catalog contains duplicate normalized model IDs")
    return tuple(result)


def rebuild_catalog_index(root: Path = DEVELOPMENT_CATALOG_ROOT) -> Path:
    packages = load_catalog_packages(root)
    value = {
        "schema": CATALOG_SCHEMA,
        "version": CATALOG_VERSION,
        "count": len(packages),
        "packages": [
            {
                "package_id": item.package_id,
                "model_id": item.model_id,
                "display_name": item.display_name,
                "visible": item.visible,
                "backend_status": item.backend_status,
            }
            for item in packages
        ],
    }
    path = root / "catalog.json"
    _atomic_write(path, _json_bytes(value))
    return path


def set_package_visibility(
    model_or_package_id: str,
    visible: bool,
    *,
    root: Path = DEVELOPMENT_CATALOG_ROOT,
) -> CatalogPackage:
    matches = [
        item
        for item in load_catalog_packages(root)
        if model_or_package_id.casefold() in {item.model_id.casefold(), item.package_id.casefold()}
    ]
    if len(matches) != 1:
        raise CatalogError(f"Expected one imported printer, found {len(matches)}: {model_or_package_id}")
    selected = matches[0]
    path = _package_directory(root, selected.package_id) / "manifest.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    value["visible"] = bool(visible)
    _atomic_write(path, _json_bytes(value))
    rebuild_catalog_index(root)
    return CatalogPackage.from_json(value)


def remove_imported_package(
    model_or_package_id: str,
    *,
    root: Path = DEVELOPMENT_CATALOG_ROOT,
    dry_run: bool = True,
) -> tuple[Path, ...]:
    """Remove one exact imported package; built-in models are never inferred."""

    matches = [
        item
        for item in load_catalog_packages(root)
        if model_or_package_id.casefold() in {item.model_id.casefold(), item.package_id.casefold()}
    ]
    if len(matches) != 1:
        raise UnsafeCatalogOperation(
            f"Removal requires one exact imported model/package ID, found {len(matches)}"
        )
    target = _package_directory(root, matches[0].package_id)
    files = tuple(sorted(path for path in target.rglob("*") if path.is_file()))
    if dry_run:
        return files
    shutil.rmtree(target)
    rebuild_catalog_index(root)
    return files


def _discover_requested_series(unified_root: Path) -> set[str]:
    """Return XP, EcoTank and SureColor packages requested for this product.

    Standard P700/P900 ESC/P-R packages are deliberately excluded. Only their
    direct-channel ``_RIP`` variants are part of the monochrome RIP catalog.
    """

    registry = json.loads((unified_root / "_registry.json").read_text(encoding="utf-8"))
    selected: set[str] = set()
    excluded = {
        "Epson_SureColorP700",
        "Epson_SureColorP900",
        # The direct-channel XP15000series package is imported instead. The
        # ESCPR package would duplicate it without per-channel control.
        "Epson_XP15000_ESCPR",
        "Epson_XP15000uvblack",
        # These are alternative presets for the same six-channel ET-8550
        # hardware. The complete ET-8550 descriptor already exposes all paths;
        # calibration and QUAD files define the ink configuration at runtime.
        "Epson_EcoTank_ET8550_RIP",
        "Epson_EcoTank_ET8550_piezography",
        # XP-750 is the built-in, capture-validated descriptor. Importing a
        # second marketing alias would create two GUI entries for one device.
        "Epson_XP750series",
        # XP-6000 currently has only an ESC/P-R RGB descriptor. It must not be
        # advertised as independently addressable ESC i until that separate
        # backend is implemented.
        "Epson_XP6000series",
    }
    for item in registry.get("printers", ()):
        package = str(item.get("package") or "")
        name = str(item.get("name") or "")
        if name in excluded:
            continue
        source_path = unified_root / package / "printer.json"
        if not source_path.is_file():
            continue
        source = json.loads(source_path.read_text(encoding="utf-8"))
        protocol_class = str((source.get("protocol") or {}).get("cls") or "")
        direct_channels = bool(source.get("channels"))
        requested = name.startswith(
            ("Epson_XP", "Epson_EcoTank", "Epson_SureColor")
        ) or (
            package.startswith("gp--")
            and any(token in name for token in (" XP-", " XP ", "EcoTank", "SureColor"))
        )
        if not requested:
            continue
        # Only an implemented direct-channel path may enter the product
        # catalog. RGB-only and unresolved old-protocol descriptors remain in
        # the knowledge base but are not advertised as printable models.
        #
        # В GUI попадают только модели с доказанной поканальной адресацией.
        implemented = (
            package.startswith("gp--")
            or (protocol_class == "escp2-esci" and direct_channels)
            or package in _BACKEND_DATA_PACKAGES
            or package in {
                "pf--Epson_SureColorP700_RIP",
                "pf--Epson_SureColorP900_RIP",
            }
        )
        if implemented:
            selected.add(_safe_package_id(package))
    return selected


def _qtr_base_tokens(qtr_root: Path) -> tuple[str, ...]:
    tokens: set[str] = set()
    if not qtr_root.is_dir():
        return ()
    for path in qtr_root.iterdir():
        if not path.is_dir() or not path.name.casefold().startswith("quad"):
            continue
        tokens.add(path.name[4:].split("-", 1)[0].casefold())
    return tuple(sorted(tokens))


def _discover_reference_driver_packages(unified_root: Path, qtr_root: Path) -> set[str]:
    """Map installed QuadToneRIP model directories to explicit unified IDs."""

    aliases = {
        # Quad1430 targets the Artisan 1430 / Stylus Photo 1500W platform.
        # Stylus Photo 1400 is its predecessor and uses a different model XML.
        "1430": "gp--escp2-artisan1430",
        "p5000": "gp--escp2-p5000-s",
        "p700": "pf--Epson_SureColorP700_RIP",
        "p708": "pf--Epson_SureColorP700_RIP",
        "p900": "pf--Epson_SureColorP900_RIP",
        "p908": "pf--Epson_SureColorP900_RIP",
    }
    selected: set[str] = set()
    missing: list[str] = []
    for token in _qtr_base_tokens(qtr_root):
        package = aliases.get(token, f"gp--escp2-{token}")
        if (unified_root / package).is_dir():
            selected.add(package)
        else:
            missing.append(token)
    if missing:
        raise CatalogError("QTR models have no explicit unified mapping: " + ", ".join(missing))
    return selected


def discover_supported_packages(
    unified_root: Path = DEFAULT_UNIFIED_ROOT,
    qtr_root: Path = DEFAULT_QTR_ROOT,
) -> tuple[str, ...]:
    """Return one normalized product catalog, independent of data origin.

    Reference-driver coverage and the requested current Epson series are only
    discovery inputs. Duplicate representations of the same physical model are
    collapsed before anything enters the pyToneRIP catalog.
    """

    selected = _discover_requested_series(unified_root)
    selected.update(_discover_reference_driver_packages(unified_root, qtr_root))
    # Primary descriptors retain the product identity and model options. Exact
    # model-XML counterparts supply the complete raster mechanics, therefore a
    # second GUI entry for the same physical printer would add no capability.
    for primary, duplicate in (
        ("pf--Epson_SureColorP400", "gp--escp2-p400"),
        ("pf--Epson_SureColorP400", "gp--escp2-p400-s"),
        ("pf--Epson_SureColorP600", "gp--escp2-p600"),
        ("pf--Epson_SureColorP600", "gp--escp2-p600-s"),
        ("pf--Epson_SureColorP800", "gp--escp2-p800"),
        ("pf--Epson_SureColorP800", "gp--escp2-p800-s"),
        ("pf--Epson_XP820series", "gp--escp2-xp820"),
        ("pf--Epson_XP15000series", "gp--escp2-xp15000"),
    ):
        if primary in selected:
            selected.discard(duplicate)
    # XP-960 is a built-in model with validated page/routing behavior.
    selected.discard("gp--escp2-xp960")
    return tuple(sorted(selected))


def resolve_supported_package(
    identity: str,
    *,
    unified_root: Path = DEFAULT_UNIFIED_ROOT,
    qtr_root: Path = DEFAULT_QTR_ROOT,
) -> str:
    """Resolve one exact package/model/display identity without fuzzy matching."""

    matches: list[str] = []
    needle = identity.casefold()
    for package_id in discover_supported_packages(unified_root, qtr_root):
        source = json.loads(
            (unified_root / package_id / "printer.json").read_text(encoding="utf-8")
        )
        candidates = {
            package_id.casefold(),
            _normalized_model_id(source, package_id).casefold(),
            _display_name(source).casefold(),
        }
        if needle in candidates:
            matches.append(package_id)
    if len(matches) != 1:
        raise CatalogError(
            f"Expected one exact supported Epson model, found {len(matches)}: {identity}"
        )
    return matches[0]


def import_package_set(
    package_ids: Iterable[str],
    *,
    unified_root: Path = DEFAULT_UNIFIED_ROOT,
    catalog_root: Path = DEVELOPMENT_CATALOG_ROOT,
    device_settings_root: Path | None = None,
    visible: bool = True,
    force: bool = False,
) -> tuple[CatalogPackage, ...]:
    result: list[CatalogPackage] = []
    for package_id in sorted(set(package_ids)):
        result.append(
            import_unified_package(
                package_id,
                unified_root=unified_root,
                catalog_root=catalog_root,
                device_settings_root=device_settings_root,
                visible=visible,
                force=force,
            )
        )
    return tuple(result)
