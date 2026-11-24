# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only
"""Read-only printer telemetry, independent of rasterization and Windows PSA.

Protocol comparison: https://github.com/Ircama/epson_print_conf
The implementation uses the project's existing BDC wire layout; no vendor
configuration tables, EEPROM commands or external application code are bundled.
"""

from __future__ import annotations

from dataclasses import dataclass

BDC_HEADER = b"@BDC ST2\r\n"
STATUS_REQUEST = b"st\x01\x00\x01"
_INK_NAMES = {
    1: "K", 3: "C", 4: "M", 5: "Y", 6: "LC", 7: "LM", 10: "LK",
    11: "MK", 12: "R", 13: "B", 14: "GO", 15: "LLK", 16: "OR", 17: "G", 22: "LK",
}
_STATES = {
    0: "error", 1: "self_printing", 2: "busy", 3: "waiting", 4: "idle",
    5: "paused", 7: "cleaning", 8: "not_initialized", 10: "shutdown",
    15: "nozzle_check", 17: "charging",
}


@dataclass(frozen=True)
class InkLevel:
    name: str
    percent: int | None
    kind: int | None = None
    color: int | None = None
    raw_level: int | None = None


@dataclass(frozen=True)
class PrinterStatus:
    inks: tuple[InkLevel, ...] = ()
    state: str | None = None
    state_code: int | None = None
    error_code: int | None = None
    warnings: tuple[int, ...] = ()
    serial_number: str | None = None
    job_name: str | None = None
    # Keep all TLVs, including new model-specific fields, as lossless hex.
    fields: tuple[tuple[int, str], ...] = ()
    source: str = "bdc"

    @property
    def ready(self) -> bool | None:
        return None if self.state_code is None else self.state_code in (3, 4)


def bdc_frame_length(data: bytes) -> int | None:
    """Length including the optional zero envelope; None until header is complete."""
    offset = int(data.startswith(b"\x00"))
    header = data[offset:offset + len(BDC_HEADER)]
    if not BDC_HEADER.startswith(header):
        raise ValueError("Invalid Epson BDC ST2 header")
    if len(data) < offset + 12:
        return None
    return offset + 12 + int.from_bytes(data[offset + 10:offset + 12], "little")


def parse_bdc_status(reply: bytes, *, source: str = "bdc") -> PrinterStatus:
    """Validate the complete TLV body before exposing any cartridge data."""
    expected = bdc_frame_length(reply)
    if expected is None or len(reply) < expected:
        raise ValueError("Incomplete Epson BDC ST2 reply")
    if reply[expected:] not in (b"", b"\x0c"):
        raise ValueError("Unexpected data after Epson BDC ST2 reply")
    position = 12 + int(reply.startswith(b"\x00"))
    blocks: list[tuple[int, bytes]] = []
    while position < expected:
        if position + 2 > expected:
            raise ValueError("Truncated BDC field header")
        tag, length = reply[position:position + 2]
        position += 2
        if position + length > expected:
            raise ValueError("Truncated BDC field")
        value = reply[position:position + length]
        if tag == 15 and (not value or value[0] < 3 or (length - 1) % value[0]):
            raise ValueError("Invalid BDC ink record width")
        blocks.append((tag, value))
        position += length
    records = [value[i:i + value[0]] for tag, value in blocks if tag == 15
               for i in range(1, len(value), value[0])]
    matte = any(record[0] == 11 for record in records)
    inks = tuple(InkLevel(
        name="PK" if record[0] == 1 and matte else _INK_NAMES.get(record[0], f"Ink {record[0]:02X}"),
        percent=0 if record[2] == ord("i") else record[2] if record[2] <= 100 else None,
        kind=record[0], color=record[1], raw_level=record[2],
    ) for record in records)
    values = dict(blocks)
    state = values.get(1, b"")
    error = values.get(2, b"")
    serial = values.get(0x40, values.get(0x1f, b"")).rstrip(b"\x00")
    job_fields = [value.strip(b"\x00") for tag, value in blocks if tag == 0x19]
    job_name = None
    if len(job_fields) == 1:
        name = job_fields[0]
        if name and name.lower() != b"unknown" and all(32 <= byte < 127 for byte in name):
            job_name = name.decode("ascii")
    return PrinterStatus(
        inks=inks,
        state=_STATES.get(state[0], "unknown") if state else None,
        state_code=state[0] if state else None,
        error_code=error[0] if error else None,
        warnings=tuple(values.get(4, b"")),
        serial_number=serial.decode("ascii", errors="replace") if serial else None,
        job_name=job_name,
        fields=tuple((tag, value.hex()) for tag, value in blocks),
        source=source,
    )


def read_network_status(host: str, *, timeout: float = 2.0, community: str = "public") -> PrinterStatus:
    from xp750_rip.printer_status_snmp import query_network_status

    return query_network_status(host, timeout=timeout, community=community)


def read_usb_status(device: str, *, timeout: float = 8.0) -> PrinterStatus:
    """Query an explicitly selected Linux usblp character device (/dev/usb/lpN)."""
    from xp750_rip.printer_status_usb import LinuxUsbPrinter, query_usb_status

    with LinuxUsbPrinter(device) as transport:
        return query_usb_status(transport, timeout=timeout)
