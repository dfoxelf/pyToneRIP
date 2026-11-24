# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Immediate Epson maintenance operations and a profiled A4 tonal test page.

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

from xp750_rip.escp2 import commands
from xp750_rip.printers import default_registry
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


def _render_test_page(request: Mapping[str, object], output_dir: Path, *, model=None) -> tuple[bytes, Path]:
    from xp750_rip.tonal_test_page import render_tonal_test_job

    return render_tonal_test_job(request, output_dir, model=model)


def execute_maintenance_request(
    request: Mapping[str, object],
    *,
    output_dir: Path,
    transport: Transport | None = None,
) -> MaintenanceExecutionResult:
    """Validate, encode and transmit one explicitly requested operation."""

    _exact_keys(
        request,
        required={"schema", "version", "action", "model"},
        optional={
            "host", "port", "connection_kind", "usb_device_id", "configuration_json",
            "printer_queue", "printer_queue_driver", "exclusive_usb",
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
    kind = _text(request.get("connection_kind", "network"), "connection_kind")
    exclusive_usb = request.get("exclusive_usb", False)
    if not isinstance(exclusive_usb, bool) or (exclusive_usb and (kind != "usb" or action != "nozzle_check")):
        raise ValueError("exclusive_usb is only supported for a USB nozzle check")
    if kind == "usb":
        from xp750_rip.windows_usb_transport import WindowsUsbInterfaceTransport, _WindowsUsbPrintApi

        device_id = _text(request.get("usb_device_id"), "usb_device_id")
        if not device_id.casefold().startswith("usbprint\\"):
            raise ValueError("A USBPRINT device instance is required")
        if exclusive_usb:
            selected_transport = transport or WindowsUsbInterfaceTransport(
                device_id, api_factory=lambda: _WindowsUsbPrintApi(exclusive=True),
            )
        else:
            selected_transport = transport or WindowsUsbInterfaceTransport(device_id)
    elif kind == "windows_queue":
        from xp750_rip.windows_printer_transport import WindowsPrinterTransport

        queue = _text(request.get("printer_queue"), "printer_queue")
        driver = _text(request.get("printer_queue_driver"), "printer_queue_driver")
        destination = WindowsPrinterTransport(queue, expected_driver=driver)
        selected_transport = transport or destination
    elif kind == "network":
        host = _text(request.get("host"), "host")
        port = _integer(request.get("port"), "port")
        if not 1 <= port <= 65535:
            raise ValueError("maintenance network port is invalid")
        selected_transport = transport or RawTcpTransport(host, port)
    else:
        raise ValueError("maintenance connection kind is invalid")

    prn_path: Path | None = None
    if action == "test_page":
        data, prn_path = _render_test_page(request, output_dir, model=model)
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
