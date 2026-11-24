# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only
"""Bounded SNMP v1/v2c GET/GETNEXT telemetry using only the standard library."""

from __future__ import annotations

import math
import secrets
import socket
from dataclasses import replace
from time import monotonic

from xp750_rip.printer_status import InkLevel, PrinterStatus, parse_bdc_status

BDC_OID = "1.3.6.1.4.1.1248.1.2.2.1.1.1.4"
STATUS_OID = "1.3.6.1.4.1.1248.1.2.2.44.1.1.2.1.115.116.1.0.1"
_SUPPLIES = "1.3.6.1.2.1.43.11.1.1."


def _tlv(tag: int, data: bytes) -> bytes:
    size = len(data)
    length = bytes([size]) if size < 128 else (
        bytes([0x80 + (size.bit_length() + 7) // 8]) +
        size.to_bytes((size.bit_length() + 7) // 8, "big"))
    return bytes([tag]) + length + data


def _integer(value: int) -> bytes:
    size = max(1, (value.bit_length() + 8) // 8)
    return _tlv(2, value.to_bytes(size, "big", signed=True))


def _base128(value: int) -> bytes:
    parts = [value & 127]
    while value > 127:
        value >>= 7
        parts.insert(0, (value & 127) | 128)
    return bytes(parts)


def _oid(oid: str) -> bytes:
    parts = tuple(map(int, oid.split(".")))
    if (len(parts) < 2 or parts[0] not in (0, 1, 2) or
            any(i < 0 or i > 0xffffffff for i in parts) or
            (parts[0] < 2 and parts[1] > 39)):
        raise ValueError("Invalid SNMP OID")
    return _tlv(6, _base128(parts[0] * 40 + parts[1]) + b"".join(map(_base128, parts[2:])))


def _elements(data: bytes) -> list[tuple[int, bytes]]:
    result = []
    pos = 0
    while pos < len(data):
        if pos + 2 > len(data):
            raise ValueError("Truncated SNMP element")
        tag, length = data[pos:pos + 2]
        pos += 2
        if length & 128:
            count = length & 127
            if not 1 <= count <= 2 or pos + count > len(data):
                raise ValueError("Invalid SNMP length")
            length = int.from_bytes(data[pos:pos + count], "big")
            pos += count
        if pos + length > len(data):
            raise ValueError("Truncated SNMP value")
        result.append((tag, data[pos:pos + length]))
        pos += length
    return result


def _number(item: tuple[int, bytes]) -> int:
    tag, value = item
    if tag != 2 or not 1 <= len(value) <= 4:
        raise ValueError("Invalid SNMP integer")
    return int.from_bytes(value, "big", signed=True)


def _decode_oid(data: bytes) -> str:
    parts = []
    number = 0
    for value in data:
        number = (number << 7) | (value & 127)
        if number > 0x10000004f:
            raise ValueError("SNMP OID arc overflow")
        if not value & 128:
            parts.append(number)
            number = 0
    if not parts or not data or data[-1] & 128:
        raise ValueError("Invalid SNMP OID")
    first = min(2, parts[0] // 40)
    return ".".join(map(str, [first, parts[0] - first * 40, *parts[1:]]))


def _response(data: bytes, request_id: int, version: int, community: bytes) -> list[tuple[str, int, bytes]]:
    outer = _elements(data)
    if len(outer) != 1 or outer[0][0] != 0x30:
        raise ValueError("Invalid SNMP envelope")
    fields = _elements(outer[0][1])
    if (len(fields) != 3 or _number(fields[0]) != version or
            fields[1] != (4, community) or fields[2][0] != 0xa2):
        raise ValueError("Unrelated SNMP response")
    pdu = _elements(fields[2][1])
    if (len(pdu) != 4 or _number(pdu[0]) != request_id or _number(pdu[1]) != 0 or
            _number(pdu[2]) != 0 or pdu[3][0] != 0x30):
        raise ValueError("Failed or unrelated SNMP request")
    result = []
    for tag, value in _elements(pdu[3][1]):
        pair = _elements(value)
        if tag != 0x30 or len(pair) != 2 or pair[0][0] != 6:
            raise ValueError("Invalid SNMP binding")
        result.append((_decode_oid(pair[0][1]), pair[1][0], pair[1][1]))
    return result


class _Client:
    def __init__(self, host: str, timeout: float, community: str):
        if not host or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("A host and finite positive status timeout are required")
        self.host = host
        self.deadline = monotonic() + timeout
        self.request_timeout = min(timeout / 8, 0.5)
        self.community = community.encode("ascii")

    def query(self, version: int, operation: int, *oids: str) -> list[tuple[str, int, bytes]]:
        # Only GET/GETNEXT exist here. No SET, EEPROM or service mutation API.
        if operation not in (0xa0, 0xa1):
            raise ValueError("Only read-only SNMP operations are allowed")
        remaining = self.deadline - monotonic()
        if remaining <= 0:
            return []
        request_id = secrets.randbelow(0x7ffffffe) + 1
        bindings = b"".join(_tlv(0x30, _oid(oid) + _tlv(5, b"")) for oid in oids)
        request = _tlv(0x30, _integer(version) + _tlv(4, self.community) + _tlv(
            operation, _integer(request_id) + _integer(0) + _integer(0) + _tlv(0x30, bindings)))
        try:
            # UDP connect restricts replies to the selected peer, including its port.
            for family, kind, protocol, _, address in socket.getaddrinfo(
                    self.host, 161, type=socket.SOCK_DGRAM):
                try:
                    with socket.socket(family, kind, protocol) as connection:
                        remaining = self.deadline - monotonic()
                        if remaining <= 0:
                            return []
                        connection.settimeout(min(remaining, self.request_timeout))
                        connection.connect(address)
                        connection.send(request)
                        response = _response(connection.recv(65535), request_id, version, self.community)
                        return response if len(response) == len(oids) else []
                except (OSError, ValueError):
                    continue
        except OSError:
            pass
        return []


def _supplies(client: _Client) -> tuple[InkLevel, ...]:
    columns = (5, 6, 8, 9)  # Supply type, description, maximum, current level.
    for version in (0, 1):
        rows: dict[str, dict[int, object]] = {}
        active = {column: _SUPPLIES + str(column) for column in columns}
        for _ in range(32):
            if not active:
                break
            response = client.query(version, 0xa1, *active.values())
            if len(response) != len(active):
                break
            following = {}
            for column, (oid, tag, data) in zip(active, response, strict=True):
                prefix = _SUPPLIES + str(column) + "."
                if (not oid.startswith(prefix) or
                        tuple(map(int, oid.split("."))) <= tuple(map(int, active[column].split(".")))):
                    continue
                if tag == 4 and column == 6:
                    value = data.decode("utf-8", errors="replace").rstrip("\0")
                elif tag in (2, 0x41, 0x42) and 1 <= len(data) <= 4:
                    value = int.from_bytes(data, "big", signed=tag == 2)
                else:
                    continue
                rows.setdefault(oid[len(prefix):], {})[column] = value
                following[column] = oid
            active = following
        inks = []
        for key in sorted(rows, key=lambda key: tuple(map(int, key.split(".")))):
            row = rows[key]
            if row.get(5) not in (5, 6):
                continue
            maximum, level = row.get(8, -1), row.get(9, -1)
            percent = (max(0, min(100, level * 100 // maximum))
                       if isinstance(maximum, int) and isinstance(level, int) and maximum > 0 and level >= 0
                       else None)
            inks.append(InkLevel(str(row.get(6) or f"Supply {key}"), percent))
        if inks:
            return tuple(inks)
    return ()


def query_network_status(host: str, *, timeout: float = 2.0, community: str = "public") -> PrinterStatus:
    """Cached BDC, direct st GET, then bounded Printer-MIB fallback.

    timeout bounds socket exchanges as a whole; OS hostname resolution may take
    longer. Missing supplies remain unknown rather than being reported as zero.
    """
    client = _Client(host, timeout, community)
    bdc = None
    for oid, operation in ((BDC_OID, 0xa1), (STATUS_OID, 0xa0)):
        for version in (0, 1):
            for returned_oid, tag, data in client.query(version, operation, oid):
                match = returned_oid == oid if operation == 0xa0 else returned_oid.startswith(oid + ".")
                if tag != 4 or not match:
                    continue
                try:
                    candidate = parse_bdc_status(data, source="snmp-bdc")
                except ValueError:
                    continue
                def quality(status):
                    return (len(status.inks), sum(i.percent is not None for i in status.inks),
                            sum(not i.name.startswith("Ink ") for i in status.inks))

                if bdc is None or quality(candidate) > quality(bdc):
                    bdc = candidate
                if bdc.inks and all(i.percent is not None and not i.name.startswith("Ink ") for i in bdc.inks):
                    return bdc
    supplies = _supplies(client)
    if supplies and (bdc is None or (len(supplies) >= len(bdc.inks) and
            sum(i.percent is not None for i in supplies) >= sum(i.percent is not None for i in bdc.inks))):
        return replace(bdc or PrinterStatus(), inks=supplies, source="snmp-printer-mib")
    if bdc is not None:
        return bdc
    raise OSError("The printer returned no supported SNMP status or ink supplies")
