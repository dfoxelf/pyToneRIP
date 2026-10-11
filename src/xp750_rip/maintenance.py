# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Immediate Epson maintenance operations and a small native test page.

The module is deliberately independent from the ordinary photo job queue.
Firmware maintenance commands do not use QTR curves, while the test page is
rendered through the selected model backend and validated before transport.
Команды обслуживания не используют QTR-кривые; тестовый PRN проходит обычный
модельный backend и проверку native-потока перед отправкой.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from xp750_rip.analysis.native_validation import (
    validate_escp2_native,
    validate_xp750_native,
)
from xp750_rip.direction import parse_print_direction
from xp750_rip.escp2 import commands
from xp750_rip.escp2.backend import EpsonEscp2Backend
from xp750_rip.printers import default_registry
from xp750_rip.raster import NamedRasterPlane, PhysicalPathId, PhysicalRasterSet
from xp750_rip.transport import RawTcpTransport, TransmissionReceipt, Transport

MAINTENANCE_REQUEST_SCHEMA = "pytone-rip.maintenance-request"
MAINTENANCE_REQUEST_VERSION = 1


@dataclass(frozen=True, slots=True)
class MaintenanceExecutionResult:
    action: str
    model_id: str
    byte_count: int
    destination: str | None
    prn_path: Path | None = None


def _exact_keys(
    value: Mapping[str, object],
    *,
    required: set[str],
    optional: set[str] = frozenset(),
) -> None:
    missing = required - set(value)
    unknown = set(value) - required - set(optional)
    if missing:
        raise ValueError("maintenance request is missing: " + ", ".join(sorted(missing)))
    if unknown:
        raise ValueError("maintenance request has unknown fields: " + ", ".join(sorted(unknown)))


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise TypeError(f"{name} must be a non-empty trimmed string")
    return value


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    return value


def build_remote_maintenance_stream(action: str) -> bytes:
    """Encode a complete standalone maintenance job.

    Framing and payloads match Gutenprint ``escputil.c``: exit IEEE-1284.4
    packet mode, reset, enter REMOTE1, emit CH or VI/NC/NC, use the six-byte
    REMOTE1 trailer, then finish the job with FF/ESC 0/ESC 0. / Последовательность побайтово повторяет открытую
    реализацию утилиты обслуживания Epson.
    """

    if action == "head_clean":
        payload = commands.remote_head_clean()
    elif action == "nozzle_check":
        payload = commands.remote_nozzle_check()
    else:
        raise ValueError(f"maintenance action has no firmware command: {action}")
    return b"".join(
        (
            commands.ejl_packet_mode_exit(),
            commands.reset(),
            commands.remote_mode_enter(),
            payload,
            commands.remote_mode_exit(),
            b"\x1b\x00",
            b"\x0c\x1b\x00\x1b\x00",
        )
    )


def _packed_step_rows(width: int, bit_depth: int) -> tuple[bytes, ...]:
    """Create one conservative four-step row for every sample bit plane."""

    maximum = (1 << bit_depth) - 1
    sample_codes = (max(1, maximum // 3), max(1, maximum * 2 // 3), maximum)
    row_bytes = width // 8
    segment_bytes = max(1, (row_bytes - 4) // len(sample_codes))
    rows: list[bytes] = []
    for bit in range(bit_depth):
        row = bytearray(row_bytes)
        for index, sample in enumerate(sample_codes):
            if sample & (1 << bit):
                start = 2 + index * segment_bytes
                end = min(row_bytes - 2, start + segment_bytes)
                row[start:end] = b"\xff" * (end - start)
        rows.append(bytes(row))
    return tuple(rows)


def _simple_test_rasters(
    *,
    mode: Any,
    physical_paths: tuple[PhysicalPathId, ...],
) -> PhysicalRasterSet:
    """Build a small raw per-path density strip without color conversion.

    This first test page intentionally checks transport, model framing, weave,
    physical routing and all configured channels. It does not claim to test
    QTR tone curves; a photographic gradient page belongs to a later module.
    """

    if not physical_paths:
        raise ValueError("test page requires at least one active physical path")
    width = max(64, round(mode.horizontal_dpi * 25.0 / 25.4))
    width = (width + 7) // 8 * 8
    height = max(len(physical_paths) * 8, round(mode.vertical_dpi * 20.0 / 25.4))
    zero = bytes(width // 8)
    active_rows = _packed_step_rows(width, mode.bit_depth)
    planes: list[NamedRasterPlane] = []
    for index, path in enumerate(physical_paths):
        start = height * index // len(physical_paths)
        end = height * (index + 1) // len(physical_paths)
        bit_planes = tuple(
            tuple(active_rows[bit] if start <= y < end else zero for y in range(height))
            for bit in range(mode.bit_depth)
        )
        planes.append(
            NamedRasterPlane(
                identity=path,
                width_pixels=width,
                bit_plane_rows=bit_planes,
            )
        )
    return PhysicalRasterSet(tuple(planes))


def _mapping(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError(f"{name} must be a JSON object")
    return dict(value)


def _render_test_page(request: Mapping[str, object], output_dir: Path) -> tuple[bytes, Path]:
    model = default_registry().get(_text(request["model"], "model"))
    mode = model.mode(_text(request["mode"], "mode"))
    medium = model.medium(_text(request["media"], "media"))
    page = model.page(_text(request["page"], "page"))
    input_source = model.input_source(_text(request["input_source"], "input_source"))

    logical_values = request.get("logical_inks")
    if not isinstance(logical_values, list) or not logical_values:
        raise TypeError("logical_inks must be a non-empty JSON array")
    logical = tuple(_text(item, "logical_inks[]") for item in logical_values)
    if len(set(logical)) != len(logical):
        raise ValueError("logical_inks must be unique")
    ink_configuration = request.get("ink_configuration")
    if ink_configuration is not None:
        ink_configuration = _text(ink_configuration, "ink_configuration")
    active_logical = model.active_logical_ink_names(logical, ink_configuration)

    routing = _mapping(request.get("routing"), "routing")
    if set(routing) != set(logical):
        raise ValueError("routing must map every and only requested logical ink")
    known_paths = {item.identity for item in model.physical_paths}
    paths: list[PhysicalPathId] = []
    for ink in active_logical:
        path = PhysicalPathId(_text(routing[ink], f"routing.{ink}"))
        if path not in known_paths:
            raise ValueError(f"routing.{ink} selects an unknown physical path: {path}")
        if path not in paths:
            paths.append(path)

    settings_raw = _mapping(request.get("device_settings", {}), "device_settings")
    settings: dict[str, str] = {}
    for setting_id, choice_id in settings_raw.items():
        setting = _text(setting_id, "device_settings key")
        choice = _text(choice_id, f"device_settings.{setting}")
        model.device_setting(setting).choice(choice)
        settings[setting] = choice

    rasters = _simple_test_rasters(mode=mode, physical_paths=tuple(paths))
    backend = EpsonEscp2Backend()
    rendered = backend.render(
        model=model,
        mode=mode,
        medium=medium,
        page=page,
        input_source=input_source,
        rasters=rasters,
        direction=parse_print_direction(request["direction"]),
        job_label="PYTONERIP-TEST",
        allow_unsupported_ppd_combinations=bool(
            request.get("allow_unsupported_ppd_combinations", False)
        ),
        device_settings=settings,
    )
    validator = (
        validate_escp2_native
        if model.escp2_profile is not None and model.backend_family != backend.family
        else validate_xp750_native
    )
    validation = validator(rendered)
    if not validation.passed:
        raise ValueError("generated maintenance test page failed native validation")
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "last-maintenance-test-page.prn"
    path.write_bytes(rendered.data)
    return rendered.data, path


def execute_maintenance_request(
    request: Mapping[str, object],
    *,
    output_dir: Path,
    transport: Transport | None = None,
) -> MaintenanceExecutionResult:
    """Validate, encode and transmit one explicitly requested operation."""

    _exact_keys(
        request,
        required={"schema", "version", "action", "model", "host", "port"},
        optional={
            "mode",
            "media",
            "page",
            "input_source",
            "direction",
            "allow_unsupported_ppd_combinations",
            "device_settings",
            "ink_configuration",
            "logical_inks",
            "routing",
        },
    )
    if request["schema"] != MAINTENANCE_REQUEST_SCHEMA:
        raise ValueError("unsupported maintenance request schema")
    if _integer(request["version"], "version") != MAINTENANCE_REQUEST_VERSION:
        raise ValueError("unsupported maintenance request version")
    action = _text(request["action"], "action")
    model = default_registry().get(_text(request["model"], "model"))
    try:
        model.maintenance_action(action)
    except KeyError as error:
        raise ValueError(f"{model.identity} does not support maintenance action {action}") from error
    host = _text(request["host"], "host")
    port = _integer(request["port"], "port")
    selected_transport = transport or RawTcpTransport(host, port)

    prn_path: Path | None = None
    if action == "test_page":
        data, prn_path = _render_test_page(request, output_dir)
    else:
        data = build_remote_maintenance_stream(action)
    receipt: TransmissionReceipt = selected_transport.send(data)
    if receipt.bytes_sent != len(data):
        raise ValueError("maintenance transport reported an incomplete transmission")
    return MaintenanceExecutionResult(
        action=action,
        model_id=model.identity,
        byte_count=len(data),
        destination=receipt.destination,
        prn_path=prn_path,
    )


def execute_maintenance_file(path: Path) -> MaintenanceExecutionResult:
    """Runtime-host boundary for a helper-created JSON request file."""

    resolved = path.resolve(strict=True)
    request = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(request, dict):
        raise TypeError("maintenance request root must be a JSON object")
    return execute_maintenance_request(request, output_dir=resolved.parent)
