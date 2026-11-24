# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only
"""IEEE 1284.4 status session and a Linux usblp adapter.

Wire structures follow the existing QueueHelper and escputil/d4lib protocol
(Jean-Jacques Sarton, GPL-2.0-or-later). This module sends only the st query.
"""

from __future__ import annotations

import math
import os
import select
import stat
import sys
from time import monotonic
from typing import Protocol, Self

from xp750_rip.printer_status import (
    STATUS_REQUEST,
    PrinterStatus,
    bdc_frame_length,
    parse_bdc_status,
)


class StatusTransport(Protocol):
    """A caller-owned bidirectional transport with bounded I/O."""

    def read(self, timeout: float) -> bytes: ...
    def write(self, data: bytes, timeout: float) -> None: ...


class LinuxUsbPrinter:
    """Use the kernel's bidirectional usblp node without detaching its driver.

    Selection and access permissions belong to the caller. CUPS/other clients
    must not use the same physical printer during the status session.
    """

    def __init__(self, device: str):
        if not sys.platform.startswith("linux"):
            raise OSError("The default Python USB status adapter requires Linux usblp")
        self.fd = os.open(device, os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            if not stat.S_ISCHR(os.fstat(self.fd).st_mode):
                raise ValueError("USB status requires a character device")
        except BaseException:
            os.close(self.fd)
            self.fd = -1
            raise

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info) -> None:
        os.close(self.fd)
        self.fd = -1

    def read(self, timeout: float) -> bytes:
        deadline = monotonic() + timeout
        while (remaining := deadline - monotonic()) > 0:
            ready, _, _ = select.select([self.fd], [], [], remaining)
            if ready:
                try:
                    data = os.read(self.fd, 65536)
                except BlockingIOError:
                    continue
                if not data:
                    raise OSError("USB printer disconnected")
                return data
        raise TimeoutError("USB printer status read timed out")

    def write(self, data: bytes, timeout: float) -> None:
        deadline = monotonic() + timeout
        pending = memoryview(data)
        while pending:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError("USB printer status write timed out")
            _, ready, _ = select.select([], [self.fd], [], remaining)
            if ready:
                try:
                    count = os.write(self.fd, pending)
                except BlockingIOError:
                    continue
                if count <= 0:
                    raise OSError("USB printer accepted no status data")
                pending = pending[count:]


class _ControlChannel:
    def __init__(self, transport: StatusTransport, timeout: float):
        self.transport = transport
        self.deadline = monotonic() + timeout
        self.buffer = bytearray()
        self.socket = 0

    def remaining(self) -> float:
        remaining = self.deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError("EPSON-CTRL status exchange timed out")
        return remaining

    def write(self, data: bytes) -> None:
        self.transport.write(data, self.remaining())

    def take(self, count: int) -> bytes:
        while len(self.buffer) < count:
            chunk = self.transport.read(self.remaining())
            if not chunk:
                raise OSError("Empty USB status transfer")
            if len(chunk) > 65536:
                raise ValueError("Oversized USB status transfer")
            self.buffer.extend(chunk)
        data = bytes(self.buffer[:count])
        del self.buffer[:count]
        return data

    def packet(self) -> bytes:
        header = self.take(6)
        length = int.from_bytes(header[2:4], "big")
        if length < 6:
            raise ValueError("Invalid IEEE 1284.4 packet length")
        return header + self.take(length - 6)

    def command(self, command: int, payload: bytes = b"", *, allow_error: bool = False) -> bytes:
        length = 7 + len(payload)
        self.write(b"\x00\x00" + length.to_bytes(2, "big") + b"\x01\x00" + bytes([command]) + payload)
        reply = self.packet()
        if len(reply) < 8 or reply[:2] != b"\0\0" or reply[6] != (command | 0x80):
            raise ValueError(f"Unexpected IEEE 1284.4 command reply: {command}")
        if reply[7] and not allow_error:
            raise OSError(f"IEEE 1284.4 command {command} rejected: {reply[7]}")
        return reply

    def credit(self) -> None:
        reply = self.command(3, bytes([self.socket, self.socket, 0, 1]))
        if len(reply) != 10 or reply[8:10] != bytes([self.socket] * 2):
            raise ValueError("Invalid EPSON-CTRL receive credit reply")

    def query(self) -> bytes:
        entered = opened = False
        failed = False
        try:
            self.write(b"\x00\x00\x00\x1b\x01@EJL 1284.4\n@EJL\n@EJL\n")
            entered = True
            self.packet()
            revision = 0x10
            init = self.command(0, bytes([revision]), allow_error=True)
            if len(init) == 9 and init[7] and init[8] == 0x20:
                revision = 0x20
                init = self.command(0, bytes([revision]))
            if len(init) != 9 or init[7] or init[8] != revision:
                raise ValueError("Unsupported IEEE 1284.4 revision")
            service = self.command(9, b"EPSON-CTRL")
            if len(service) < 9 or service[8] == 0:
                raise ValueError("EPSON-CTRL is unavailable")
            self.socket = service[8]
            sockets = bytes([self.socket] * 2)
            channel = self.command(1, sockets + b"\x02\0\x02\0\0\0" +
                                   (b"\0\0" if revision == 0x10 else b""))
            opened = True
            if (len(channel) != (16 if revision == 0x10 else 14) or
                    channel[8:10] != sockets or int.from_bytes(channel[10:12], "big") < 11 or
                    int.from_bytes(channel[12:14], "big") <= 6):
                raise ValueError("Invalid EPSON-CTRL packet sizes")
            while True:
                credit = self.command(4, sockets + (b"\0\x80\xff\xff" if revision == 0x10 else b"\0\x08"))
                if len(credit) != 12 or credit[8:10] != sockets:
                    raise ValueError("Invalid EPSON-CTRL send credit reply")
                if int.from_bytes(credit[10:12], "big"):
                    break
            self.credit()
            self.write(sockets + b"\0\x0b\0\x01" + STATUS_REQUEST)
            status = bytearray()
            while True:
                packet = self.packet()
                if packet[:2] != sockets:
                    raise ValueError("Unexpected EPSON-CTRL data socket")
                status.extend(packet[6:])
                expected = bdc_frame_length(bytes(status))
                if expected is not None and len(status) >= expected:
                    return bytes(status)  # Decoder validates any final form feed.
                self.credit()
        except BaseException:
            failed = True
            raise
        finally:
            # Cleanup has its own small deadline; never mask the query failure.
            try:
                try:
                    self.deadline = monotonic() + 1
                    if opened:
                        self.command(2, bytes([self.socket, self.socket, 0]))
                finally:
                    self.deadline = monotonic() + 1
                    if entered:
                        self.command(8)
            except (OSError, ValueError):
                if not failed:
                    raise


def query_usb_status(transport: StatusTransport, *, timeout: float = 8.0) -> PrinterStatus:
    """Query caller-owned USB I/O; compatible with future libusb/Windows adapters."""
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Status timeout must be finite and positive")
    return parse_bdc_status(_ControlChannel(transport, timeout).query(), source="usb-bdc")
