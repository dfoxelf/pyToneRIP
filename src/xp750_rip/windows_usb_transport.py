# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""RAW delivery to a present Windows USBPRINT device interface."""

from __future__ import annotations

import ctypes
import logging
import os
from collections.abc import Callable, Iterable
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path

from xp750_rip.transport import TransmissionReceipt, TransportTransmissionError
from xp750_rip.usb_streaming import BufferedUsbSender, UsbBufferPolicy

_LOGGER = logging.getLogger(__name__)


class _Guid(ctypes.Structure):
    _fields_ = [
        ("data1", wintypes.DWORD), ("data2", wintypes.WORD),
        ("data3", wintypes.WORD), ("data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def from_string(cls, value: str) -> _Guid:
        import uuid

        return cls.from_buffer_copy(uuid.UUID(value).bytes_le)


class _SpDeviceInterfaceData(ctypes.Structure):
    _fields_ = [
        ("cb_size", wintypes.DWORD), ("interface_class_guid", _Guid),
        ("flags", wintypes.DWORD), ("reserved", ctypes.c_size_t),
    ]


class _SpDevInfoData(ctypes.Structure):
    _fields_ = [
        ("cb_size", wintypes.DWORD), ("class_guid", _Guid),
        ("dev_inst", wintypes.DWORD), ("reserved", ctypes.c_size_t),
    ]


class _WindowsUsbPrintApi:
    _USBPRINT_INTERFACE = "28d78fad-5a12-11d1-ae5b-0000f803a8c2"
    _ERROR_NO_MORE_ITEMS = 259
    _DIGCF_PRESENT = 0x2
    _DIGCF_DEVICEINTERFACE = 0x10
    _GENERIC_WRITE = 0x40000000
    _FILE_SHARE_READ = 0x1
    _FILE_SHARE_WRITE = 0x2
    _OPEN_EXISTING = 3
    _FILE_ATTRIBUTE_NORMAL = 0x80
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("USBPRINT transport requires Windows")
        self.setupapi = ctypes.WinDLL("setupapi.dll", use_last_error=True)
        self.cfgmgr = ctypes.WinDLL("cfgmgr32.dll", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32.dll", use_last_error=True)
        self._declare_functions()

    def _declare_functions(self) -> None:
        self.setupapi.SetupDiGetClassDevsW.argtypes = [
            ctypes.POINTER(_Guid), wintypes.LPCWSTR, wintypes.HWND, wintypes.DWORD,
        ]
        self.setupapi.SetupDiGetClassDevsW.restype = wintypes.HANDLE
        self.setupapi.SetupDiEnumDeviceInterfaces.argtypes = [
            wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(_Guid), wintypes.DWORD,
            ctypes.POINTER(_SpDeviceInterfaceData),
        ]
        self.setupapi.SetupDiEnumDeviceInterfaces.restype = wintypes.BOOL
        self.setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(_SpDeviceInterfaceData), ctypes.c_void_p,
            wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(_SpDevInfoData),
        ]
        self.setupapi.SetupDiGetDeviceInterfaceDetailW.restype = wintypes.BOOL
        self.setupapi.SetupDiGetDeviceInstanceIdW.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(_SpDevInfoData), wintypes.LPWSTR,
            wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
        ]
        self.setupapi.SetupDiGetDeviceInstanceIdW.restype = wintypes.BOOL
        self.setupapi.SetupDiDestroyDeviceInfoList.argtypes = [wintypes.HANDLE]
        self.setupapi.SetupDiDestroyDeviceInfoList.restype = wintypes.BOOL

        self.cfgmgr.CM_Locate_DevNodeW.argtypes = [
            ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, wintypes.ULONG,
        ]
        self.cfgmgr.CM_Locate_DevNodeW.restype = wintypes.ULONG
        self.cfgmgr.CM_Get_Parent.argtypes = [
            ctypes.POINTER(wintypes.DWORD), wintypes.DWORD, wintypes.ULONG,
        ]
        self.cfgmgr.CM_Get_Parent.restype = wintypes.ULONG
        self.cfgmgr.CM_Get_Device_ID_Size.argtypes = [
            ctypes.POINTER(wintypes.ULONG), wintypes.DWORD, wintypes.ULONG,
        ]
        self.cfgmgr.CM_Get_Device_ID_Size.restype = wintypes.ULONG
        self.cfgmgr.CM_Get_Device_IDW.argtypes = [
            wintypes.DWORD, wintypes.LPWSTR, wintypes.ULONG, wintypes.ULONG,
        ]
        self.cfgmgr.CM_Get_Device_IDW.restype = wintypes.ULONG

        self.kernel.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]
        self.kernel.CreateFileW.restype = wintypes.HANDLE
        self.kernel.WriteFile.argtypes = [
            wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
        ]
        self.kernel.WriteFile.restype = wintypes.BOOL
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL
        self.kernel.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenThread.restype = wintypes.HANDLE
        self.kernel.CancelSynchronousIo.argtypes = [wintypes.HANDLE]
        self.kernel.CancelSynchronousIo.restype = wintypes.BOOL

    @staticmethod
    def _win_error(operation: str) -> OSError:
        error = ctypes.WinError(ctypes.get_last_error())
        return OSError(error.winerror, operation + ": " + str(error))

    def _parent_instance_id(self, device_id: str) -> str:
        node = wintypes.DWORD()
        result = self.cfgmgr.CM_Locate_DevNodeW(ctypes.byref(node), device_id, 0)
        if result != 0:
            raise OSError("Selected USB printer is disconnected")
        parent = wintypes.DWORD()
        result = self.cfgmgr.CM_Get_Parent(ctypes.byref(parent), node, 0)
        if result != 0:
            raise OSError("USBPRINT device has no USB parent interface")
        length = wintypes.ULONG()
        result = self.cfgmgr.CM_Get_Device_ID_Size(ctypes.byref(length), parent, 0)
        if result != 0:
            raise OSError("Could not read USB parent device identity")
        value = ctypes.create_unicode_buffer(length.value + 1)
        result = self.cfgmgr.CM_Get_Device_IDW(parent, value, length.value + 1, 0)
        if result != 0:
            raise OSError("Could not read USB parent device identity")
        return value.value.casefold()

    def _interface_path(self, device_id: str) -> str:
        parent_id = self._parent_instance_id(device_id)
        interface_guid = _Guid.from_string(self._USBPRINT_INTERFACE)
        device_set = self.setupapi.SetupDiGetClassDevsW(
            ctypes.byref(interface_guid), None, None,
            self._DIGCF_PRESENT | self._DIGCF_DEVICEINTERFACE,
        )
        if device_set == self._INVALID_HANDLE_VALUE:
            raise self._win_error("SetupDiGetClassDevs")
        try:
            index = 0
            while True:
                interface = _SpDeviceInterfaceData()
                interface.cb_size = ctypes.sizeof(interface)
                if not self.setupapi.SetupDiEnumDeviceInterfaces(
                    device_set, None, ctypes.byref(interface_guid), index, ctypes.byref(interface),
                ):
                    error = ctypes.get_last_error()
                    if error == self._ERROR_NO_MORE_ITEMS:
                        break
                    raise self._win_error("SetupDiEnumDeviceInterfaces")
                required = wintypes.DWORD()
                info = _SpDevInfoData()
                info.cb_size = ctypes.sizeof(info)
                self.setupapi.SetupDiGetDeviceInterfaceDetailW(
                    device_set, ctypes.byref(interface), None, 0, ctypes.byref(required), ctypes.byref(info),
                )
                if not required.value or required.value > 65536:
                    raise self._win_error("SetupDiGetDeviceInterfaceDetail size")
                detail = ctypes.create_string_buffer(required.value)
                ctypes.cast(detail, ctypes.POINTER(wintypes.DWORD)).contents.value = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
                if not self.setupapi.SetupDiGetDeviceInterfaceDetailW(
                    device_set, ctypes.byref(interface), detail, required.value,
                    ctypes.byref(required), ctypes.byref(info),
                ):
                    raise self._win_error("SetupDiGetDeviceInterfaceDetail")
                instance_id = ctypes.create_unicode_buffer(1024)
                if not self.setupapi.SetupDiGetDeviceInstanceIdW(
                    device_set, ctypes.byref(info), instance_id, len(instance_id), None,
                ):
                    raise self._win_error("SetupDiGetDeviceInstanceId")
                if instance_id.value.casefold() == parent_id:
                    path = ctypes.wstring_at(ctypes.addressof(detail) + 4)
                    return path
                index += 1
        finally:
            self.setupapi.SetupDiDestroyDeviceInfoList(device_set)
        raise OSError("USBPRINT interface was not found for the selected printer")

    def open(self, device_id: str) -> wintypes.HANDLE:
        path = self._interface_path(device_id)
        handle = self.kernel.CreateFileW(
            path, self._GENERIC_WRITE, self._FILE_SHARE_READ | self._FILE_SHARE_WRITE,
            None, self._OPEN_EXISTING, self._FILE_ATTRIBUTE_NORMAL, None,
        )
        if handle == self._INVALID_HANDLE_VALUE:
            raise self._win_error("Open USBPRINT interface")
        return handle

    def write(self, handle: wintypes.HANDLE, data: memoryview) -> int:
        buffer = ctypes.create_string_buffer(bytes(data))
        written = wintypes.DWORD()
        if not self.kernel.WriteFile(handle, buffer, len(data), ctypes.byref(written), None):
            raise self._win_error("Write USBPRINT interface")
        return int(written.value)

    def close(self, handle: wintypes.HANDLE) -> None:
        self.kernel.CloseHandle(handle)

    def cancel(self, thread_id: int) -> bool:
        # CancelSynchronousIo requires THREAD_TERMINATE access to the issuing
        # thread. This requests I/O cancellation; it never terminates a thread.
        thread = self.kernel.OpenThread(0x0001, False, thread_id)
        if not thread:
            if ctypes.get_last_error() == 87:  # Thread already exited.
                return False
            raise self._win_error("Open USB sender thread")
        try:
            if self.kernel.CancelSynchronousIo(thread):
                return True
            if ctypes.get_last_error() == 1168:  # No pending operation yet.
                return False
            raise self._win_error("Cancel USB synchronous I/O")
        finally:
            self.kernel.CloseHandle(thread)


@dataclass(frozen=True, slots=True)
class WindowsUsbInterfaceTransport:
    device_id: str
    connection_state: Callable[[bool], None] | None = field(default=None, repr=False, compare=False)
    api_factory: Callable = field(default=_WindowsUsbPrintApi, repr=False, compare=False)
    buffer_policy: UsbBufferPolicy = field(default_factory=UsbBufferPolicy)
    statistics: Callable[[dict], None] | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.device_id.strip().casefold().startswith("usbprint\\"):
            raise ValueError("A USBPRINT device instance is required")

    @property
    def destination(self) -> str:
        return "usbprint://" + self.device_id

    def send_from_producer(self, producer, *, cancel_check=None, progress=None) -> TransmissionReceipt:
        def connection(active: bool) -> None:
            if self.connection_state is not None:
                try:
                    self.connection_state(active)
                except Exception:
                    _LOGGER.debug("USB connection status callback failed", exc_info=True)

        session = BufferedUsbSender(
            device_id=self.device_id, api_factory=self.api_factory, policy=self.buffer_policy,
            connection=connection, cancel_check=cancel_check, progress=progress,
            statistics=self.statistics,
        )
        try:
            sent = session.run(producer)
            return TransmissionReceipt(sent, self.destination)
        except Exception as error:
            raise TransportTransmissionError(
                f"Windows USBPRINT write failed after {session.sent} bytes: {error}",
                bytes_sent=session.sent, destination=self.destination,
            ) from error

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
