# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Targeted, data-driven Gutenprint Epson printer importer."""

from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from xp750_rip.capabilities.ppd import PpdCapabilities, PpdChoice, parse_ppd, read_ppd
from xp750_rip.printers.capabilities import (
    InputSourceCapability,
    MediaCapability,
    PageCapability,
    PhysicalPathDefinition,
    PrinterMode,
    PrinterModel,
)
from xp750_rip.printers.media_mappings import (
    MEDIA_MAPPINGS,
    canonical_media_choices,
    media_display_name,
    media_material_id,
    media_print_methods,
    supported_media_modes,
)
from xp750_rip.raster import PhysicalPathId

WORKSPACE_ROOT = Path(__file__).resolve().parents[4]
GUTENPRINT_ROOT = WORKSPACE_ROOT / "gutenprint-5.3.5"
DEFINITION_DIR = Path(__file__).resolve().parents[3] / "printer_catalog" / "import-drafts"
SCHEMA_VERSION = 1
GENERATED_BY_VERSION = "EpsonRIP Phase 11"


class ImportErrorBase(ValueError):
    """Base class for deterministic importer errors."""


class ImportAmbiguousError(ImportErrorBase):
    pass


class ImportNotFoundError(ImportErrorBase):
    pass


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _text(value: str | None) -> str | None:
    return value.strip() if value else None


def _ints(value: str | None) -> tuple[int, ...]:
    return tuple(int(float(item)) for item in re.findall(r"[-+]?\d+(?:\.\d+)?", value or ""))


def _clean_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def _decode_xml_byte_string(value: str | None) -> bytes:
    """Decode Gutenprint's printable ``\\ooo`` representation.

    XML command sequences use three-digit octal escapes for non-printable
    bytes and literal ASCII for command names. Keeping this decoder at the
    import boundary lets the installed package store named mechanics instead
    of copying an opaque byte string.

    XML хранит непечатные байты как трёхзначные восьмеричные ``\\ooo``.
    Декодирование выполняется только на границе импорта; в каталог попадают
    уже понятные поля механики, а не нерасшифрованная строка байтов.
    """

    source = value or ""
    output = bytearray()
    index = 0
    while index < len(source):
        if (
            source[index] == "\\"
            and index + 3 < len(source)
            and all(character in "01234567" for character in source[index + 1 : index + 4])
        ):
            output.append(int(source[index + 1 : index + 4], 8))
            index += 4
            continue
        codepoint = ord(source[index])
        if codepoint > 0x7F:
            raise ImportErrorBase("Non-ASCII byte in an ESC/P2 XML command sequence")
        output.append(codepoint)
        index += 1
    return bytes(output)


def _remote_records(value: str | None) -> tuple[tuple[str, bytes], ...]:
    """Split a concatenated REMOTE1 setup string into checked records."""

    data = _decode_xml_byte_string(value)
    records: list[tuple[str, bytes]] = []
    index = 0
    while index < len(data):
        if index + 4 > len(data):
            raise ImportErrorBase("Truncated REMOTE1 command header in input-slot XML")
        command_bytes = data[index : index + 2]
        try:
            command = command_bytes.decode("ascii")
        except UnicodeDecodeError as error:
            raise ImportErrorBase("Non-ASCII REMOTE1 input-slot command") from error
        length = int.from_bytes(data[index + 2 : index + 4], "little")
        index += 4
        if index + length > len(data):
            raise ImportErrorBase(f"Truncated REMOTE1 {command} payload in input-slot XML")
        records.append((command, data[index : index + length]))
        index += length
    return tuple(records)


def _feed_mechanics(slot: ET.Element) -> dict[str, object]:
    """Return named sheet/roll behavior and the exact REMOTE1 paper path."""

    roll = next(iter(slot.findall("RollFeed")), None)
    roll_cut_mode = "none"
    suppress_form_feed = False
    if roll is not None:
        if next(iter(roll.findall("CutAll")), None) is not None:
            roll_cut_mode = "cut_each_page"
        elif next(iter(roll.findall("CutLast")), None) is not None:
            roll_cut_mode = "cut_last_page"
        suppress_form_feed = next(iter(roll.findall("DontEject")), None) is not None

    init = next(iter(slot.findall("InitSequence")), None)
    deinit = next(iter(slot.findall("DeinitSequence")), None)
    paper_path = None
    cutter_edge_trim_mode = None
    edge_mode_by_code = {0: "standard", 1: "single_edge", 2: "double_edge"}
    for command, payload in _remote_records(init.text if init is not None else None):
        if command == "PP":
            if len(payload) != 3 or payload[0] != 0:
                raise ImportErrorBase("REMOTE1 PP input-slot payload has an unknown layout")
            paper_path = {"type": payload[1], "number": payload[2]}
        elif command == "SN" and len(payload) == 3 and payload[:2] == b"\x00\x84":
            try:
                cutter_edge_trim_mode = edge_mode_by_code[payload[2]]
            except KeyError as error:
                raise ImportErrorBase(
                    f"Unknown input-slot cutter edge mode: {payload[2]}"
                ) from error

    # A legacy input-slot family may omit PP and configure the path through
    # IR/EX only. Preserve the historical automatic-sheet fallback for that
    # case; a present PP record always wins and is decoded exactly.
    # Старые семейства могут не иметь PP и выбирать тракт через IR/EX. Для них
    # остаётся прежний безопасный адрес автоподачи; найденный PP всегда точен.
    return {
        "medium_kind": "roll" if roll is not None else "automatic_cut_sheet",
        "roll_cut_mode": roll_cut_mode,
        "cutter_edge_trim_mode": cutter_edge_trim_mode,
        "remote_paper_path": paper_path or {"type": 1, "number": 1},
        "suppress_form_feed_after_page": suppress_form_feed,
        "initialization_commands": [
            {"command": command, "payload_hex": payload.hex()}
            for command, payload in _remote_records(init.text if init is not None else None)
        ],
        "deinitialization_commands": [
            {"command": command, "payload_hex": payload.hex()}
            for command, payload in _remote_records(deinit.text if deinit is not None else None)
        ],
    }


def _strip_namespaces(root: ET.Element) -> ET.Element:
    for item in root.iter():
        if "}" in item.tag:
            item.tag = item.tag.rsplit("}", 1)[1]
    return root


@dataclass(frozen=True, slots=True)
class GutenprintSelection:
    name: str
    driver_id: str
    model_id: str
    parameters: str | None
    source: str
    override_used: bool = False


@dataclass(frozen=True, slots=True)
class DependencyFile:
    path: str
    source_node: str
    inherited: bool
    sha256: str


@dataclass(frozen=True, slots=True)
class ResolvedDependencies:
    selection: GutenprintSelection
    files: tuple[DependencyFile, ...]
    roots: tuple[ET.Element, ...]
    unresolved: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PrinterDefinition:
    schema_version: int
    generated_by_version: str
    printer_id: str
    display_name: str
    protocol_family: str
    gutenprint_model_id: str
    gutenprint_driver_id: str
    physical_paths: tuple[dict[str, Any], ...]
    ink_metadata: tuple[dict[str, Any], ...]
    modes: tuple[dict[str, Any], ...]
    media: tuple[dict[str, Any], ...]
    pages: tuple[dict[str, Any], ...]
    input_sources: tuple[dict[str, Any], ...]
    borderless_capability: bool
    direction_capability: str | None
    compression_capability: str | None
    ppd_metadata: dict[str, str]
    ppd_source_text: str
    source_provenance: tuple[str, ...]
    source_fingerprints: dict[str, str]
    verification_status: str
    unresolved_fields: tuple[str, ...]
    source_conflicts: tuple[str, ...]
    override_used: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "generated_by_version": self.generated_by_version,
            "printer_id": self.printer_id,
            "display_name": self.display_name,
            "protocol_family": self.protocol_family,
            "gutenprint_model_id": self.gutenprint_model_id,
            "gutenprint_driver_id": self.gutenprint_driver_id,
            "physical_paths": list(self.physical_paths),
            "ink_metadata": list(self.ink_metadata),
            "modes": list(self.modes),
            "media": list(self.media),
            "pages": list(self.pages),
            "input_sources": list(self.input_sources),
            "borderless_capability": self.borderless_capability,
            "direction_capability": self.direction_capability,
            "compression_capability": self.compression_capability,
            "ppd_metadata": self.ppd_metadata,
            "ppd_source_text": self.ppd_source_text,
            "source_provenance": list(self.source_provenance),
            "source_fingerprints": self.source_fingerprints,
            "verification_status": self.verification_status,
            "unresolved_fields": list(self.unresolved_fields),
            "source_conflicts": list(self.source_conflicts),
            "override_used": self.override_used,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> PrinterDefinition:
        if data.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(f"Неподдерживаемая schema_version: {data.get('schema_version')}")
        return cls(
            schema_version=int(data["schema_version"]),
            generated_by_version=str(data["generated_by_version"]),
            printer_id=str(data["printer_id"]),
            display_name=str(data["display_name"]),
            protocol_family=str(data["protocol_family"]),
            gutenprint_model_id=str(data["gutenprint_model_id"]),
            gutenprint_driver_id=str(data["gutenprint_driver_id"]),
            physical_paths=tuple(data["physical_paths"]),
            ink_metadata=tuple(data.get("ink_metadata", ())),
            modes=tuple(data["modes"]),
            media=tuple(data["media"]),
            pages=tuple(data["pages"]),
            input_sources=tuple(data["input_sources"]),
            borderless_capability=bool(data.get("borderless_capability", False)),
            direction_capability=data.get("direction_capability"),
            compression_capability=data.get("compression_capability"),
            ppd_metadata=dict(data["ppd_metadata"]),
            ppd_source_text=str(data["ppd_source_text"]),
            source_provenance=tuple(data["source_provenance"]),
            source_fingerprints=dict(data["source_fingerprints"]),
            verification_status=str(data["verification_status"]),
            unresolved_fields=tuple(data["unresolved_fields"]),
            source_conflicts=tuple(data["source_conflicts"]),
            override_used=bool(data.get("override_used", False)),
        )


class GutenprintResolver:
    """Resolve one printer entry and only its referenced XML subtree."""

    def __init__(self, root: Path = GUTENPRINT_ROOT) -> None:
        self.root = root
        self.printers_xml = root / "src" / "xml" / "printers" / "escp2.xml"

    def resolve_printer(
        self, ppd: PpdCapabilities, *, override_model: str | None = None
    ) -> GutenprintSelection:
        if not self.printers_xml.is_file():
            raise ImportNotFoundError(f"Gutenprint escp2.xml не найден: {self.printers_xml}")
        root = _strip_namespaces(ET.parse(self.printers_xml).getroot())
        entries = [item for item in root.iter("printer") if item.get("manufacturer", "").casefold() == "epson"]
        if override_model is not None:
            matches = [item for item in entries if item.get("model") == str(override_model)]
            if not matches:
                raise ImportNotFoundError(f"Gutenprint model={override_model} не найден")
            item = matches[0]
            return GutenprintSelection(
                item.get("name", ""), item.get("driver", ""), item.get("model", ""),
                item.get("parameters"), str(self.printers_xml), True,
            )
        model_name = ppd.metadata.get("ModelName", "")
        device_id = ppd.metadata.get("1284DeviceID", "")
        exact = [
            item for item in entries
            if _clean_name(model_name) in _clean_name(item.get("name", ""))
            or item.get("deviceid", "") and _clean_name(item.get("deviceid", "")) in _clean_name(device_id)
        ]
        # Epson's XP-750/XP-960 PPDs identify the physical printer, while the
        # project explicitly maps this family to the PX700W/model96 driver.
        if not exact and any(token in _clean_name(model_name) for token in ("xp750", "xp960")):
            exact = [item for item in entries if item.get("driver") == "escp2-px700fw"]
        if not exact:
            raise ImportNotFoundError(
                f"Для PPD ModelName={model_name!r} не найден однозначный Epson Gutenprint entry"
            )
        if len(exact) != 1:
            raise ImportAmbiguousError(
                "IMPORT_AMBIGUOUS: " + ", ".join(item.get("name", "") for item in exact)
            )
        item = exact[0]
        return GutenprintSelection(
            item.get("name", ""), item.get("driver", ""), item.get("model", ""),
            item.get("parameters"), str(self.printers_xml), False,
        )

    def _path_for_reference(self, reference: str) -> Path:
        value = reference.replace("\\", "/")
        if value.startswith("escp2/"):
            return self.root / "src" / "xml" / value
        return self.root / "src" / "xml" / "escp2" / value

    def dependencies(self, selection: GutenprintSelection) -> ResolvedDependencies:
        model_path = self.root / "src" / "xml" / "escp2" / "model" / f"model_{selection.model_id}.xml"
        queue: list[tuple[Path, bool, str]] = [(model_path, False, "model")]
        seen: set[Path] = set()
        files: list[DependencyFile] = []
        roots: list[ET.Element] = []
        unresolved: list[str] = []
        while queue:
            path, inherited, source_node = queue.pop(0)
            path = path.resolve()
            if path in seen:
                continue
            seen.add(path)
            if not path.is_file():
                unresolved.append(str(path))
                continue
            root = _strip_namespaces(ET.parse(path).getroot())
            roots.append(root)
            files.append(DependencyFile(str(path), source_node, inherited, _sha256(path)))
            for element in root.iter():
                for attribute in ("base", "src"):
                    reference = element.get(attribute)
                    if reference:
                        queue.append((self._path_for_reference(reference), True, f"{element.tag}@{attribute}"))
        return ResolvedDependencies(selection, tuple(files), tuple(roots), tuple(unresolved))


def _first_element(roots: tuple[ET.Element, ...], tag: str) -> ET.Element | None:
    for root in roots:
        found = next(iter(root.iter(tag)), None)
        if found is not None:
            return found
    return None


def _mode_records(deps: ResolvedDependencies) -> tuple[dict[str, Any], ...]:
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    nozzle = _first_element(deps.roots, "headConfiguration")
    nozzle_count = int(nozzle.findtext("Nozzles", "0")) if nozzle is not None else 0
    nozzle_sep = int(nozzle.findtext("NozzleSeparation", "0")) if nozzle is not None else 0
    for root in deps.roots:
        for item in root.iter("resolution"):
            mode_id = item.get("name")
            if not mode_id or mode_id in seen:
                continue
            physical = _ints(item.findtext("physicalResolution"))
            printed = _ints(item.findtext("printedResolution")) or physical
            parameters = {p.get("name", ""): (p.get("value") or p.text or "") for p in item.findall("parameter")}
            # In Gutenprint, this value multiplies the effective ink density.
            # Explicit zero-density XML entries are not usable print modes.
            if (
                "escp2_density" in parameters
                and float(parameters["escp2_density"] or 0) <= 0
            ):
                continue
            seen.add(mode_id)
            printer_weave = item.find("printerWeave")
            weave_mode = None
            if printer_weave is not None:
                weave_command = _decode_xml_byte_string(printer_weave.get("command"))
                if (
                    len(weave_command) != 6
                    or weave_command[:5] != b"\x1b(i\x01\x00"
                ):
                    raise ImportErrorBase(
                        f"Unsupported printerWeave command for {mode_id}: {weave_command.hex()}"
                    )
                weave_mode = weave_command[5]
            drops = tuple(float(parameters[name]) for name in ("DropSize1", "DropSize2", "DropSize3") if name in parameters)
            base_resolution = int(parameters.get("escp2_base_res", "0") or 0)
            # ESC/P2 horizontal passes are normally implicit. In particular,
            # P600/P800 1440x1440ov declares a 1440-DPI printed raster on a
            # 2880-DPI printer coordinate grid with a 720-DPI base. The
            # backend therefore emits two interleaved horizontal subpasses
            # even though the XML has no <horizontalPasses> element.
            #
            # Число горизонтальных проходов обычно не записано отдельным
            # тегом. Оно выводится тем же способом, что и в native backend,
            # иначе каталог и фактически созданный PRN расходятся.
            physical_xdpi = min(base_resolution, physical[0]) if physical else 0
            derived_horizontal_passes = (
                max(1, printed[0] // physical_xdpi)
                if printed and physical_xdpi > 0
                else 1
            )
            records.append({
                "identity": mode_id,
                "display_name": item.get("text", mode_id),
                "horizontal_dpi": printed[0] if printed else 0,
                "vertical_dpi": printed[1] if len(printed) > 1 else 0,
                "physical_horizontal_dpi": physical[0] if physical else 0,
                "physical_vertical_dpi": physical[1] if len(physical) > 1 else 0,
                "base_resolution_dpi": base_resolution,
                "horizontal_passes": int(
                    item.findtext("horizontalPasses", str(derived_horizontal_passes))
                ),
                "vertical_passes": int(item.findtext("verticalPasses", "1")),
                "nozzle_count": nozzle_count,
                "nozzle_separation": nozzle_sep,
                "bit_depth": int(parameters.get("escp2_bits", "0") or 0),
                "dot_size": int(parameters.get("escp2_ink_type", "0") or 0),
                "printer_weave_mode": weave_mode,
                "drop_sizes": drops,
                "density": float(parameters.get("escp2_density", "0") or 0),
                "source": next((f.path for f in deps.files if f.path.endswith("model_" + deps.selection.model_id + ".xml")), deps.files[0].path),
                "backend_status": (
                    "BACKEND_READY"
                    if deps.selection.model_id == "96" and mode_id != "360x90sw"
                    else "IMPORTED_INCOMPLETE"
                ),
            })
    return tuple(records)


def _page_records(ppd: PpdCapabilities) -> tuple[dict[str, Any], ...]:
    return tuple({
        "name": page.name, "label": page.label, "width_points": page.width_points,
        "height_points": page.height_points, "imageable_left_points": page.imageable_left_points,
        "imageable_bottom_points": page.imageable_bottom_points,
        "imageable_right_points": page.imageable_right_points,
        "imageable_top_points": page.imageable_top_points,
        "borderless": page.borderless,
    } for page in ppd.pages)


def _input_records(ppd: PpdCapabilities, deps: ResolvedDependencies) -> tuple[dict[str, Any], ...]:
    option = next((item for item in ppd.options if item.name == "InputSlot"), None)
    if option is not None:
        return tuple({
            "identity": choice.name, "display_name": choice.label,
            "source": "PPD", "implemented": False,
        } for choice in option.choices)
    return tuple({
        "identity": slot.get("name", "UNKNOWN"), "display_name": slot.get("text", slot.get("name", "UNKNOWN")),
        "source": next((file.path for file in deps.files if "inputslots" in file.path), "Gutenprint XML"),
        "implemented": False,
    } for root in deps.roots for slot in root.iter("slot"))


def _physical_paths(deps: ResolvedDependencies) -> tuple[dict[str, Any], ...]:
    element = _first_element(deps.roots, "physicalChannels")
    count = int(element.text or "0") if element is not None else 0
    names = [item.get("name") for root in deps.roots for item in root.iter("ChannelName")]
    return tuple({"identity": f"P{index}", "factory_role": names[index] if index < len(names) else None} for index in range(count))


def _ink_metadata(deps: ResolvedDependencies) -> tuple[dict[str, Any], ...]:
    result: list[dict[str, Any]] = []
    for root in deps.roots:
        ink_name = next(iter(root.iter("InkName")), None)
        if ink_name is None:
            continue
        for channel in ink_name.iter("channel"):
            for sub in channel.findall("subchannel"):
                result.append({
                    "channel": channel.get("name"), "index": channel.get("index"),
                    "name": sub.get("name"), "color": sub.get("color"),
                    "subchannel": sub.get("subchannel"), "head_offset": sub.get("headOffset"),
                    "selector": None,
                })
    return tuple(result)


def _remote_payload(
    mechanics: dict[str, object], command_name: str
) -> str | None:
    commands = mechanics.get("initialization_commands")
    if not isinstance(commands, list):
        return None
    for command in commands:
        if isinstance(command, dict) and command.get("command") == command_name:
            value = command.get("payload_hex")
            return value if isinstance(value, str) else None
    return None


def _normalize_pro_cut_sheet_source(feeds: list[dict[str, object]]) -> None:
    """Translate Gutenprint's Pro auto-select slot to the physical sheet path.

    The Pro cutter-and-roll input profile exposes both a generic ``Standard``
    slot (``PP 00 01 FF``) and a manual slot (``PP 00 02 01``). QTR's
    user-facing ``Sheet`` choice for this profile selects the third semantic
    option: the physical sheet path, ``PP 00 02 00``. The 7890 and 9890 QTR
    streams independently confirm that mapping. Detect the complete input
    profile rather than a printer identity, so every model that imports the
    same protocol receives the same sheet-feed semantics.
    """

    standard = next((feed for feed in feeds if feed.get("name") == "Standard"), None)
    manual = next((feed for feed in feeds if feed.get("name") == "Manual"), None)
    if not isinstance(standard, dict) or not isinstance(manual, dict):
        return
    mechanics = standard.get("feed_mechanics")
    manual_mechanics = manual.get("feed_mechanics")
    if not isinstance(mechanics, dict) or not isinstance(manual_mechanics, dict):
        return
    if (
        mechanics.get("remote_paper_path") != {"type": 1, "number": 255}
        or _remote_payload(mechanics, "PP") != "0001ff"
        or manual_mechanics.get("remote_paper_path") != {"type": 2, "number": 1}
        or _remote_payload(manual_mechanics, "PP") != "000201"
    ):
        return

    mechanics["remote_paper_path"] = {"type": 2, "number": 0}
    commands = mechanics["initialization_commands"]
    assert isinstance(commands, list)
    for command in commands:
        assert isinstance(command, dict)
        if command.get("command") == "PP":
            command["payload_hex"] = "000200"
            standard["text"] = "Sheet Feed"
            return
    raise ImportErrorBase("Pro cut-sheet profile has no PP initialization command")


def resolved_gutenprint_catalog_data(
    dependencies: ResolvedDependencies,
) -> dict[str, object]:
    """Flatten an inherited Gutenprint XML graph for a self-contained package.

    The upstream model files use ``base`` and ``src`` references extensively;
    reading only ``model_N.xml`` loses resolutions, heads, inks and media. This
    adapter walks the guarded dependency graph and stores only runtime mechanics
    required by pyToneRIP.

    Разрешённая структура записывается при импорте. На пользовательской машине
    исходное дерево Gutenprint больше не требуется.
    """

    modes = list(_mode_records(dependencies))
    inksets: list[dict[str, object]] = []
    for root in dependencies.roots:
        for ink_list in root.iter("InkList"):
            configuration_id = ink_list.get("name") or f"configuration-{len(inksets)}"
            configuration_name = ink_list.get("text") or configuration_id
            for ink in ink_list.findall("InkName"):
                channels: list[dict[str, object]] = []
                for channel in ink.iter("channel"):
                    for subchannel in channel.findall("subchannel"):
                        color = int(subchannel.get("color", "0"))
                        sub = int(subchannel.get("subchannel", "-1"))
                        selector = color if sub < 0 else color | (sub << 4)
                        channels.append(
                            {
                                "slot": subchannel.get("name"),
                                "role": (subchannel.get("name") or "unknown")
                                .casefold()
                                .replace(" ", "_"),
                                "esc_i_code": f"0x{selector:02x}",
                                "subchannel": sub,
                                "head_offset": int(subchannel.get("headOffset", "0")),
                            }
                        )
                if channels:
                    inksets.append(
                        {
                            "configuration_id": configuration_id,
                            "configuration_name": configuration_name,
                            "name": ink.get("name") or f"inkset-{len(inksets)}",
                            "display_name": ink.get("text") or ink.get("name"),
                            "channels": channels,
                        }
                    )

    paper_types: list[dict[str, object]] = []
    for root in dependencies.roots:
        for paper in root.iter("paper"):
            parameters: dict[str, list[str]] = {}
            for parameter in paper.iter("parameter"):
                name = parameter.get("name")
                if name:
                    parameters.setdefault(name, []).append(
                        (parameter.text or "").strip()
                    )
            if parameters:
                paper_types.append(
                    {
                        "name": paper.get("name"),
                        "text": paper.get("text", ""),
                        "class": paper.get("class", ""),
                        "params": parameters,
                    }
                )

    feeds: list[dict[str, object]] = []
    for root in dependencies.roots:
        for slot in root.iter("slot"):
            parameters = {
                parameter.get("name"): (parameter.text or "").strip()
                for parameter in slot.iter("parameter")
                if parameter.get("name")
            }
            feeds.append(
                {
                    "name": slot.get("name"),
                    "text": slot.get("text", ""),
                    "params": parameters,
                    "feed_mechanics": _feed_mechanics(slot),
                }
            )

    media_sizes: list[dict[str, object]] = []
    for root in dependencies.roots:
        for size in root.iter("MediaSize"):
            code = next(
                (
                    int((parameter.text or "0").strip())
                    for parameter in size.iter("parameter")
                    if parameter.get("name") == "PaperMediaSize"
                ),
                None,
            )
            media_sizes.append(
                {
                    "name": size.get("name"),
                    "type": size.get("type", "sheet"),
                    "paper_media_size_code": code,
                }
            )

    # Device features are declarations of commands supported by the selected
    # model/media family. Keep only stable identifiers here; command encoding
    # remains in the backend capability layer.
    #
    # Возможности устройства наследуются через XML-граф так же, как режимы и
    # головки. Без этого импортированная модель теряла параметры сушки.
    device_features = sorted(
        {
            str(feature.get("name"))
            for root in dependencies.roots
            for feature in root.iter("feature")
            if feature.get("name")
        }
    )
    base_separation = _first_element(dependencies.roots, "baseSeparation")
    base_separation_dpi = int(
        base_separation.text
        if base_separation is not None and base_separation.text
        else "360"
    )
    command_set = _first_element(dependencies.roots, "commandSet")
    if command_set is not None and command_set.text == "Pro":
        _normalize_pro_cut_sheet_source(feeds)
    maximum_resolution = _first_element(dependencies.roots, "maximumResolution")
    maximum_media_size = _first_element(dependencies.roots, "maximumMediaSize")
    minimum_media_size = _first_element(dependencies.roots, "minimumMediaSize")
    margin_records: dict[tuple[str, str], dict[str, object]] = {}
    for root in dependencies.roots:
        for margin in root.iter("margins"):
            if margin.get("duplex") == "duplex":
                continue
            key = (margin.get("interleave", "soft"), margin.get("media", "sheet"))
            values = [float(value) for value in (margin.text or "").split()]
            if len(values) != 4:
                raise ImportErrorBase(f"Invalid Gutenprint margins: {margin.text}")
            # Dependencies are visited child first: the nearest declaration wins.
            margin_records.setdefault(key, {
                "interleave": key[0], "media": key[1], "points": values,
            })

    return {
        "schema": "pytone-rip.gutenprint-runtime",
        "version": 1,
        "model_id": dependencies.selection.model_id,
        "base_separation_dpi": base_separation_dpi,
        "command_set": command_set.text if command_set is not None else None,
        "unit_scale": max(_ints(maximum_resolution.text)) if maximum_resolution is not None else 5760,
        "maximum_media_size_points": list(_ints(maximum_media_size.text))
        if maximum_media_size is not None else [],
        "minimum_media_size_points": list(_ints(minimum_media_size.text))
        if minimum_media_size is not None else [],
        "page_margins": list(margin_records.values()),
        "interchangeable_ink": any(
            next(iter(root.iter("hasInterchangeableInkCartridges")), None)
            is not None
            for root in dependencies.roots
        ),
        "modes": modes,
        "inksets": inksets,
        "paper_types": paper_types,
        "feeds": feeds,
        "media_sizes": media_sizes,
        "device_features": device_features,
    }


def _media_equivalence_key(
    ppd: PpdCapabilities, choice: PpdChoice
) -> tuple[object, ...]:
    """Deduplicate only choices with equal native mapping and PPD constraints."""

    identity = choice.name
    mapping = MEDIA_MAPPINGS.get(identity)
    if mapping is None:
        return ("UNKNOWN", media_material_id(identity))
    allowed_pages = tuple(
        page.name
        for page in ppd.pages
        if not ppd.validate_choices(
            {"MediaType": identity, "PageSize": page.name}
        )
    )
    return (*mapping, allowed_pages)


def normalize_import(
    ppd_path: Path, *, gutenprint_root: Path = GUTENPRINT_ROOT,
    override_model: str | None = None,
) -> PrinterDefinition:
    ppd = read_ppd(ppd_path)
    resolver = GutenprintResolver(gutenprint_root)
    selection = resolver.resolve_printer(ppd, override_model=override_model)
    deps = resolver.dependencies(selection)
    model_name = ppd.metadata.get("ModelName", "Epson Imported")
    printer_stem = re.sub(r"\s+Series$", "", model_name, flags=re.IGNORECASE)
    printer_stem = re.sub(r"^Epson\s+", "", printer_stem, flags=re.IGNORECASE)
    printer_stem = re.sub(r"^Stylus\s+Photo\s+", "", printer_stem, flags=re.IGNORECASE)
    printer_id = re.sub(r"[^A-Za-z0-9]+", "", printer_stem) or f"GP{selection.model_id}"
    physical_paths = _physical_paths(deps)
    modes = _mode_records(deps)
    media_choices, _media_aliases = canonical_media_choices(
        ppd.option("MediaType").choices,
        equivalence_key=lambda choice: _media_equivalence_key(ppd, choice),
    )
    media = tuple({
        "identity": choice.name, "display_name": media_display_name(choice.label),
        "source": str(ppd_path), "implemented": False,
        "backend_status": "IMPORTED_INCOMPLETE",
    } for choice in media_choices)
    inputs = _input_records(ppd, deps)
    unresolved = list(deps.unresolved)
    if not physical_paths:
        unresolved.append("physicalChannels")
    if not modes:
        unresolved.append("resolutions")
    fingerprints = {
        "ppd_sha256": _sha256(ppd_path), "gutenprint_version": "5.3.5",
        "model_id": selection.model_id, "printers_xml_sha256": _sha256(resolver.printers_xml),
    }
    fingerprints.update({f"xml:{Path(item.path).name}": item.sha256 for item in deps.files})
    return PrinterDefinition(
        SCHEMA_VERSION, GENERATED_BY_VERSION, printer_id, model_name,
        "epson_escp2_model96" if selection.model_id == "96" else "epson_escp2_imported",
        selection.model_id, selection.driver_id, physical_paths, _ink_metadata(deps), modes, media,
        _page_records(ppd), inputs, any(page.borderless for page in ppd.pages), None, None,
        dict(ppd.metadata), ppd_path.read_text(encoding="latin-1"),
        (str(ppd_path), selection.source, *(item.path for item in deps.files)), fingerprints,
        "IMPORTED", tuple(unresolved), (), selection.override_used,
    )


def definition_to_model(definition: PrinterDefinition) -> PrinterModel:
    ppd = parse_ppd(definition.ppd_source_text)
    modes = tuple(PrinterMode(
        identity=item["identity"], display_name=item["display_name"],
        horizontal_dpi=int(item["horizontal_dpi"]), vertical_dpi=int(item["vertical_dpi"]),
        physical_horizontal_dpi=int(item["physical_horizontal_dpi"]), physical_vertical_dpi=int(item["physical_vertical_dpi"]),
        base_resolution_dpi=int(item["base_resolution_dpi"]), horizontal_passes=int(item["horizontal_passes"]),
        vertical_passes=int(item["vertical_passes"]), nozzle_count=int(item["nozzle_count"]),
        nozzle_separation=int(item["nozzle_separation"]), bit_depth=int(item["bit_depth"]),
        dot_size=int(item["dot_size"]), drop_sizes=tuple(item["drop_sizes"]), density=float(item["density"]),
        source=str(item["source"]), implemented=item["backend_status"] == "BACKEND_READY",
        offline_validated=False,
        blocked_reason=None if item["backend_status"] == "BACKEND_READY" else "IMPORTED_INCOMPLETE: backend data requires validation",
    ) for item in definition.modes)
    pages = tuple(
        PageCapability(
            ppd=page,
            implemented=True,
            offline_validated=True,
        )
        for page in ppd.pages
    )
    mode_ids = tuple(item.identity for item in modes)
    media_items: list[MediaCapability] = []
    media_choices, media_aliases = canonical_media_choices(
        ppd.option("MediaType").choices,
        equivalence_key=lambda choice: _media_equivalence_key(ppd, choice),
    )
    for item in media_choices:
        mapped = MEDIA_MAPPINGS.get(item.name)
        if mapped is None:
            media_items.append(MediaCapability(
                item.name, media_display_name(item.label),
                str(definition.source_provenance[0]), False,
                blocked_reason="Native ESC/P2 media mapping remains unknown.",
            ))
            continue
        gutenprint_name, media_code, family = mapped
        supported_modes = supported_media_modes(item.name, mode_ids)
        is_cd = item.name.startswith("CDDVD")
        media_items.append(MediaCapability(
            identity=item.name,
            display_name=media_display_name(item.label),
            source=str(definition.source_provenance[0]),
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
        ))
    media = tuple(media_items)
    inputs = tuple(InputSourceCapability(
        str(item["identity"]), str(item["display_name"]), str(item.get("source", definition.source_provenance[0])),
        False, "IMPORTED_INCOMPLETE"
    ) for item in definition.input_sources)
    return PrinterModel(
        definition.printer_id, definition.display_name, definition.protocol_family,
        tuple(PhysicalPathDefinition(PhysicalPathId(item["identity"]), item.get("factory_role")) for item in definition.physical_paths),
        modes, media, pages, inputs, ppd, definition.source_provenance, media_aliases,
    )


def persist_definition(definition: PrinterDefinition, *, directory: Path = DEFINITION_DIR, force: bool = False) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{definition.printer_id.casefold()}.json"
    if path.exists() and not force:
        raise FileExistsError(f"Импортированное определение уже существует: {definition.printer_id}")
    path.write_text(json.dumps(definition.to_json(), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def load_persisted_models(*, directory: Path = DEFINITION_DIR) -> tuple[PrinterModel, ...]:
    if not directory.is_dir():
        return ()
    models: list[PrinterModel] = []
    for path in sorted(directory.glob("*.json")):
        models.append(definition_to_model(PrinterDefinition.from_json(json.loads(path.read_text(encoding="utf-8")))))
    return tuple(models)


def import_report(definition: PrinterDefinition) -> dict[str, Any]:
    ready = sum(item.get("backend_status") == "BACKEND_READY" for item in definition.modes)
    return {
        "printer_id": definition.printer_id, "display_name": definition.display_name,
        "gutenprint_model": definition.gutenprint_model_id,
        "gutenprint_driver": definition.gutenprint_driver_id,
        "physical_paths": len(definition.physical_paths),
        "ink_topology": list(definition.ink_metadata),
        "modes": len(definition.modes), "backend_ready_modes": ready,
        "incomplete_modes": len(definition.modes) - ready,
        "media": len(definition.media), "pages": len(definition.pages),
        "input_sources": len(definition.input_sources),
        "borderless": definition.borderless_capability,
        "direction": definition.direction_capability or "UNKNOWN",
        "compression": definition.compression_capability or "UNKNOWN",
        "verification_status": definition.verification_status,
        "unresolved_fields": list(definition.unresolved_fields),
        "source_conflicts": list(definition.source_conflicts),
    }


def compare_imported_to_model(definition: PrinterDefinition, model: PrinterModel) -> dict[str, list[str]]:
    """Compare stable normalized identities for the XP750 round-trip oracle."""

    imported = {
        "physical_paths": [item["identity"] for item in definition.physical_paths],
        "modes": [item["identity"] for item in definition.modes],
        "media": [item["identity"] for item in definition.media],
        "pages": [item["name"] for item in definition.pages],
        "input_sources": [item["identity"] for item in definition.input_sources],
    }
    existing = {
        "physical_paths": [item.identity.value for item in model.physical_paths],
        "modes": [item.identity for item in model.modes],
        "media": [item.identity for item in model.media],
        "pages": [item.ppd.name for item in model.pages],
        "input_sources": [item.identity for item in model.input_sources],
    }
    report: dict[str, list[str]] = {}
    for field, imported_values in imported.items():
        left, right = set(imported_values), set(existing[field])
        report[field] = [
            "MATCH" if left == right else "DIFFERENCE",
            *(f"MISSING:{item}" for item in sorted(right - left)),
            *(f"EXTRA:{item}" for item in sorted(left - right)),
        ]
    return report
