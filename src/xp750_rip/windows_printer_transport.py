# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Progressive RAW delivery through an existing Windows printer queue.

Only the desktop runtime calls this module. Windows keeps ownership of USB.
Successful writes mean spooler acceptance, not completed physical printing.
"""

from __future__ import annotations

import ctypes
import os
import re
from collections.abc import Callable, Iterable
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from xp750_rip.transport import TransmissionReceipt, TransportTransmissionError


class _DocInfo(ctypes.Structure):
    _fields_ = [("name", wintypes.LPWSTR), ("output", wintypes.LPWSTR), ("datatype", wintypes.LPWSTR)]


class _PrinterInfo(ctypes.Structure):
    _fields_ = [(name, wintypes.LPWSTR) for name in (
        "server", "name", "share", "port", "driver", "comment", "location",
    )] + [("devmode", ctypes.c_void_p), ("separator", wintypes.LPWSTR),
         ("processor", wintypes.LPWSTR), ("datatype", wintypes.LPWSTR),
         ("parameters", wintypes.LPWSTR), ("security", ctypes.c_void_p)] + [
             (name, wintypes.DWORD) for name in (
                 "attributes", "priority", "default_priority", "start", "until", "status", "jobs", "ppm",
             )]


class _WindowsPrinterApi:
    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("Windows printer queue transport requires Windows")
        self.dll = ctypes.WinDLL("winspool.drv", use_last_error=True)
        declarations = {
            "OpenPrinterW": ([wintypes.LPWSTR, ctypes.POINTER(wintypes.HANDLE), ctypes.c_void_p], wintypes.BOOL),
            "GetPrinterW": ([wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
            "StartDocPrinterW": ([wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(_DocInfo)], wintypes.DWORD),
            "WritePrinter": ([wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
            "EndDocPrinter": ([wintypes.HANDLE], wintypes.BOOL),
            "AbortPrinter": ([wintypes.HANDLE], wintypes.BOOL),
            "ClosePrinter": ([wintypes.HANDLE], wintypes.BOOL),
            "SetJobW": ([wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
        }
        for name, (arguments, result) in declarations.items():
            function = getattr(self.dll, name)
            function.argtypes, function.restype = arguments, result

    @staticmethod
    def _error(operation: str) -> OSError:
        error = ctypes.WinError(ctypes.get_last_error())
        return OSError(error.winerror, operation + ": " + str(error))

    def open(self, queue: str, port: str | None, device_id: str | None, expected_driver: str | None = None) -> wintypes.HANDLE:
        self.device_id = None
        handle = wintypes.HANDLE()
        if not self.dll.OpenPrinterW(queue, ctypes.byref(handle), None):
            raise self._error("OpenPrinter")
        try:
            needed = wintypes.DWORD()
            self.dll.GetPrinterW(handle, 2, None, 0, ctypes.byref(needed))
            if not needed.value or needed.value > 1024 * 1024:
                raise self._error("GetPrinter size")
            buffer = ctypes.create_string_buffer(needed.value)
            if not self.dll.GetPrinterW(handle, 2, buffer, len(buffer), ctypes.byref(needed)):
                raise self._error("GetPrinter")
            info = ctypes.cast(buffer, ctypes.POINTER(_PrinterInfo)).contents
            if port and (info.port or "").casefold() != port.casefold():
                raise ValueError("Physical queue no longer uses the selected USB port")
            if (info.driver or "").casefold() in {
                "microsoft virtual print class driver", "microsoft ipp class driver", "microsoft print to pdf",
            } or (info.name or "").casefold().startswith("pytonerip"):
                raise ValueError("Select a physical RAW USB queue, not a virtual or IPP queue")
            if (info.processor or "").casefold() != "winprint" or (info.datatype or "").casefold() != "raw":
                raise ValueError("Printer queue must use winprint and RAW without appended form feeds")
            if info.separator or info.attributes & 2:
                raise ValueError("Printer queue must spool without separator pages")
            if expected_driver is not None:
                if (info.driver or "").casefold() != expected_driver.casefold():
                    raise ValueError("The installed Windows queue driver has changed")
                excluded = (
                    "pytone-rip:", "pytonerip.", "file:", "portprompt:", "nul:",
                    "wdp", "wsd", "ipp:", "ipps:",
                )
                ports = [name.strip() for name in (info.port or "").casefold().split(",")]
                if info.status & 4 or any(not name or name.startswith(excluded) for name in ports):
                    raise ValueError("The Windows queue is not on a supported physical printer port")
                return handle
            cfg = ctypes.WinDLL("cfgmgr32.dll")
            locate = cfg.CM_Locate_DevNodeW
            locate.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, wintypes.ULONG]
            locate.restype = wintypes.ULONG
            node = wintypes.DWORD()
            if locate(ctypes.byref(node), device_id, 0) != 0:
                raise OSError("Selected USB printer is disconnected")
            self.device_id = device_id
            return handle
        except Exception:
            self.dll.ClosePrinter(handle)
            raise

    def start(self, handle: wintypes.HANDLE, name: str) -> int:
        info = _DocInfo(name, None, "RAW")
        job_id = self.dll.StartDocPrinterW(handle, 1, ctypes.byref(info))
        if not job_id:
            raise self._error("StartDocPrinter")
        return int(job_id)

    def write(self, handle: wintypes.HANDLE, data: memoryview) -> int:
        buffer = ctypes.create_string_buffer(bytes(data))
        written = wintypes.DWORD()
        if not self.dll.WritePrinter(handle, buffer, len(data), ctypes.byref(written)):
            raise self._error("WritePrinter")
        return int(written.value)

    def finish(self, handle: wintypes.HANDLE) -> None:
        if not self.dll.EndDocPrinter(handle):
            raise self._error("EndDocPrinter")

    def abort(self, handle: wintypes.HANDLE, job_id: int) -> bool:
        # Stop the owned spool job before resetting data already at the device.
        deleted = bool(self.dll.AbortPrinter(handle))
        deleted = bool(self.dll.SetJobW(handle, job_id, 0, None, 5)) or deleted
        if not deleted:
            return False
        if self.device_id is None:
            return True
        from xp750_rip.windows_usb_transport import _WindowsUsbPrintApi
        usb = _WindowsUsbPrintApi()
        device = usb.open(self.device_id)
        try:
            return usb.abort(device, b"")
        finally:
            usb.close(device)

    def close(self, handle: wintypes.HANDLE) -> None:
        self.dll.ClosePrinter(handle)


@dataclass(frozen=True, slots=True)
class WindowsPrinterTransport:
    queue_name: str
    usb_port: str | None = None
    device_id: str | None = None
    connection_state: Callable[[bool], None] | None = field(default=None, repr=False, compare=False)
    job_state: Callable[[int, str, str], None] | None = field(default=None, repr=False, compare=False)
    api_factory: Callable = field(default=_WindowsPrinterApi, repr=False, compare=False)
    expected_driver: str | None = None

    def __post_init__(self) -> None:
        if not self.queue_name.strip() or self.queue_name.casefold().startswith("pytonerip"):
            raise ValueError("A physical Windows queue is required")
        if self.expected_driver is not None:
            if not self.expected_driver.strip().casefold().startswith("epson "):
                raise ValueError("An installed Epson Windows queue is required")
            if self.usb_port is not None or self.device_id is not None:
                raise ValueError("Windows queue delivery cannot open a USB interface")
            return
        if self.usb_port is None or self.device_id is None:
            raise ValueError("A Windows USB port and device instance are required")
        if not re.fullmatch(r"USB\d+", self.usb_port, re.IGNORECASE):
            raise ValueError("A Windows USB printer port is required")
        if not self.device_id.casefold().startswith("usbprint\\"):
            raise ValueError("A USBPRINT device instance is required")
        if self.device_id.rsplit("&", 1)[-1].casefold() != self.usb_port.casefold():
            raise ValueError("USB device and port do not match")

    @property
    def destination(self) -> str:
        return "windows_queue://" + self.queue_name

    def send_from_producer(self, producer, *, cancel_check=None, progress=None) -> TransmissionReceipt:
        api = None
        handle = None
        job_id = 0
        sent = 0
        name = "pyToneRIP " + uuid4().hex
        abort_attempted = abort_succeeded = False

        def state(value: str) -> None:
            if self.job_state is not None:
                self.job_state(job_id, name, value)

        def connection(active: bool) -> None:
            if self.connection_state is not None:
                try:
                    self.connection_state(active)
                except Exception:
                    pass

        try:
            if cancel_check is not None:
                cancel_check()

            def write(chunk: bytes) -> None:
                nonlocal api, handle, job_id, sent
                if cancel_check is not None:
                    cancel_check()
                if not isinstance(chunk, bytes) or not chunk:
                    raise ValueError("live PRN producer must yield non-empty bytes")
                if handle is None:
                    api = self.api_factory()
                    if self.expected_driver is None:
                        handle = api.open(self.queue_name, self.usb_port, self.device_id)
                    else:
                        handle = api.open(self.queue_name, None, None, self.expected_driver)
                    job_id = api.start(handle, name)
                    state("ACTIVE")
                    connection(True)
                remaining = memoryview(chunk)
                while remaining:
                    if cancel_check is not None:
                        cancel_check()
                    part = remaining[:65536]
                    written = api.write(handle, part)
                    if written <= 0 or written > len(part):
                        raise OSError("WritePrinter returned an invalid byte count")
                    sent += written
                    remaining = remaining[written:]
                    if progress is not None:
                        progress(sent)

            producer(write)
            if handle is None or not sent:
                raise ValueError("live PRN producer emitted no data")
            if cancel_check is not None:
                cancel_check()
            api.finish(handle)
            # EndDocPrinter accepted the job. Monitoring must not report a
            # delivery failure that could encourage duplicate physical printing.
            try:
                state("SPOOLED")
            except Exception:
                pass
            return TransmissionReceipt(sent, self.destination)
        except Exception as error:
            if handle is not None and job_id:
                abort_attempted = True
                try:
                    abort_succeeded = api.abort(handle, job_id)
                except OSError:
                    pass
                try:
                    state("ABORTED" if abort_succeeded else "ABORT_FAILED")
                except OSError:
                    pass
            raise TransportTransmissionError(
                f"Windows RAW job failed after {sent} bytes: {error}",
                bytes_sent=sent, destination=self.destination,
                abort_attempted=abort_attempted, abort_succeeded=abort_succeeded,
            ) from error
        finally:
            if handle is not None:
                try:
                    api.close(handle)
                finally:
                    connection(False)

    def send_chunks(self, chunks: Iterable[bytes], *, cancel_check=None, progress=None) -> TransmissionReceipt:
        def produce(write):
            for chunk in chunks:
                write(chunk)
        return self.send_from_producer(produce, cancel_check=cancel_check, progress=progress)

    def send(self, data: bytes) -> TransmissionReceipt:
        return self.send_chunks((data,))

    def send_file(self, path: Path, *, chunk_size=1024 * 1024, progress=None, cancel_check=None) -> TransmissionReceipt:
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        total = path.stat().st_size
        def chunks():
            with path.open("rb") as source:
                while chunk := source.read(chunk_size):
                    yield chunk
        return self.send_chunks(chunks(), cancel_check=cancel_check,
            progress=None if progress is None else lambda sent: progress(sent, total))
