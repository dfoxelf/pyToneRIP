# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only
"""Explicit physical cancellation, separate from read-only printer telemetry."""

import math
import secrets
import socket
from time import sleep

from xp750_rip.printer_status_snmp import _integer, _oid, _response, _tlv

CANCEL_OID = "1.3.6.1.4.1.1248.1.2.2.44.1.1.2.1.114.115.1.0.1"


def cancel_network_print(host: str, *, timeout: float = 2.0) -> None:
    """Send EPSON-CTRL rs and require its acknowledgement.

    Despite using an SNMP GET envelope, this command changes device state.
    Only a validated BUSY response permits another attempt.
    """
    if not host or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("A host and finite positive cancellation timeout are required")
    binding = _tlv(0x30, _oid(CANCEL_OID) + _tlv(5, b""))
    # Choose one address before sending; do not repeat a side-effecting command
    # on another address if the first reply is missing or invalid.
    family, kind, protocol, _, address = socket.getaddrinfo(host, 161, type=socket.SOCK_DGRAM)[0]
    with socket.socket(family, kind, protocol) as connection:
        connection.settimeout(timeout)
        connection.connect(address)
        for attempt in range(3):
            if attempt:
                sleep(0.5)
            request_id = secrets.randbelow(0x7ffffffe) + 1
            packet = _tlv(0x30, _integer(0) + _tlv(4, b"public") + _tlv(
                0xa0, _integer(request_id) + _integer(0) + _integer(0) + _tlv(0x30, binding)))
            connection.send(packet)
            response = _response(connection.recv(65535), request_id, 0, b"public")
            if response in ([(CANCEL_OID, 4, b"\0rs:01:OK;\x0c")],
                            [(CANCEL_OID, 4, b"rs:01:OK;\x0c")]):
                return
            if response not in ([(CANCEL_OID, 4, b"\0rs:01:BUSY;\x0c")],
                                [(CANCEL_OID, 4, b"rs:01:BUSY;\x0c")]):
                raise OSError("The printer did not acknowledge physical print cancellation")
    raise OSError("The printer continued to return BUSY for physical print cancellation")
