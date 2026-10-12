# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

import pytest

from xp750_rip.runtime_host import _output_transport, run_request
from xp750_rip.transport import RawTcpTransport, TransportTransmissionError
from xp750_rip.windows_printer_transport import WindowsPrinterTransport
from xp750_rip.windows_usb_transport import WindowsUsbInterfaceTransport

DEVICE = r"USBPRINT\EPSONSC-P800_SERIES\7&example&0&USB004"


class FakeSpooler:
    def __init__(self, *, limit=3, fail_at=None, open_error=None, finish_error=False):
        self.limit = limit
        self.fail_at = fail_at
        self.open_error = open_error
        self.finish_error = finish_error
        self.data = bytearray()
        self.calls = []
        self.writes = 0
        self.after_write = None

    def open(self, queue, port, device):
        self.calls.append(("open", queue, port, device))
        if self.open_error:
            raise self.open_error
        return 10

    def start(self, handle, name):
        self.calls.append(("start", handle, name))
        return 42

    def write(self, handle, data):
        self.writes += 1
        if self.writes == self.fail_at:
            raise OSError("printer queue disappeared")
        count = min(len(data), self.limit)
        self.data.extend(data[:count])
        self.calls.append(("write", count))
        if self.after_write:
            self.after_write()
        return count

    def finish(self, handle):
        self.calls.append(("finish", handle))
        if self.finish_error:
            raise OSError("EndDocPrinter failed")

    def abort(self, handle, job_id):
        self.calls.append(("abort", handle, job_id))
        return True

    def close(self, handle):
        self.calls.append(("close", handle))


def transport(api, **kwargs):
    return WindowsPrinterTransport("Epson physical P800", "USB004", DEVICE,
        api_factory=lambda: api, **kwargs)


class FakeUsbInterface:
    def __init__(self, *, limit=4, fail_at=None):
        self.limit = limit
        self.fail_at = fail_at
        self.data = bytearray()
        self.calls = []

    def open(self, device):
        self.calls.append(("open", device))
        return 12

    def write(self, handle, data):
        self.calls.append(("write", handle, len(data)))
        if self.fail_at == len([call for call in self.calls if call[0] == "write"]):
            raise OSError("USB interface write failed")
        count = min(self.limit, len(data))
        self.data.extend(data[:count])
        return count

    def close(self, handle):
        self.calls.append(("close", handle))


def direct_transport(api, **kwargs):
    return WindowsUsbInterfaceTransport(DEVICE, api_factory=lambda: api, **kwargs)


def test_direct_usb_interface_streams_raw_bytes_and_reports_progress():
    api = FakeUsbInterface(limit=3)
    states = []
    progress = []
    receipt = direct_transport(api, connection_state=states.append).send_chunks(
        [b"\x1b@\x00header", b"\x00raster\xff", b"\x0c"], progress=progress.append)
    expected = b"\x1b@\x00header\x00raster\xff\x0c"
    assert bytes(api.data) == expected
    assert receipt.bytes_sent == len(expected)
    assert receipt.destination.startswith("usbprint://USBPRINT")
    assert progress[-1] == len(expected)
    assert states == [True, False]
    assert api.calls[0] == ("open", DEVICE)
    assert api.calls[-1] == ("close", 12)


def test_direct_usb_interface_failure_reports_partial_bytes_without_retry():
    api = FakeUsbInterface(limit=2, fail_at=2)
    with pytest.raises(TransportTransmissionError) as failure:
        direct_transport(api).send(b"abcdef")
    assert bytes(api.data) == b"ab"
    assert failure.value.bytes_sent == 2
    assert [call[0] for call in api.calls].count("open") == 1
    assert api.calls[-1] == ("close", 12)


def test_direct_usb_interface_rejects_empty_payload_without_opening():
    api = FakeUsbInterface()
    with pytest.raises(TransportTransmissionError, match="emitted no data"):
        direct_transport(api).send_chunks([])
    assert api.calls == []


def test_direct_usb_interface_rejects_non_usbprint_device():
    with pytest.raises(ValueError):
        WindowsUsbInterfaceTransport("USB\\VID_1234&PID_5678")


def test_progressive_chunks_handle_partial_writes_and_embedded_zeroes():
    api = FakeSpooler(limit=2)
    events = []
    progress = []
    def produce(write):
        write(b"\x1b@\x00header")
        assert bytes(api.data) == b"\x1b@\x00header"
        events.append("first chunk completed")
        write(b"\x00raster\xff")
        write(b"\x0c")
    receipt = transport(api, job_state=lambda *state: events.append(state)).send_from_producer(produce, progress=progress.append)
    assert bytes(api.data) == b"\x1b@\x00header\x00raster\xff\x0c"
    assert receipt.bytes_sent == len(api.data)
    assert progress[-1] == receipt.bytes_sent
    assert events[0][2] == "ACTIVE" and events[-1][2] == "SPOOLED"
    assert api.calls[-2:] == [("finish", 10), ("close", 10)]
    assert not any(call[0] == "abort" for call in api.calls)


@pytest.mark.parametrize("chunk", [b"", bytearray(b"abc"), "abc"])
def test_invalid_input_does_not_create_a_physical_job(chunk):
    api = FakeSpooler()
    with pytest.raises(TransportTransmissionError):
        transport(api).send_chunks([chunk])
    assert api.calls == []


def test_empty_job_does_not_create_a_physical_job():
    api = FakeSpooler()
    with pytest.raises(TransportTransmissionError, match="emitted no data"):
        transport(api).send_chunks([])
    assert api.calls == []


@pytest.mark.parametrize("written", [0, -1, 100])
def test_invalid_native_write_count_aborts_only_this_job(written):
    api = FakeSpooler()
    api.write = lambda handle, data: written
    with pytest.raises(TransportTransmissionError) as failure:
        transport(api).send(b"abcd")
    assert failure.value.bytes_sent == 0
    assert ("abort", 10, 42) in api.calls
    assert api.calls[-1] == ("close", 10)
    assert not any(call[0] == "finish" for call in api.calls)


def test_error_during_write_reports_bytes_without_retry():
    api = FakeSpooler(limit=2, fail_at=2)
    with pytest.raises(TransportTransmissionError) as failure:
        transport(api).send(b"abcdef")
    assert bytes(api.data) == b"ab"
    assert failure.value.bytes_sent == 2
    assert failure.value.abort_attempted and failure.value.abort_succeeded
    assert sum(call[0] == "start" for call in api.calls) == 1
    assert ("abort", 10, 42) in api.calls


def test_cancellation_during_stream_aborts_owned_job():
    api = FakeSpooler(limit=2)
    cancelled = False
    class Cancelled(Exception):
        pass
    def request_cancel():
        nonlocal cancelled
        cancelled = True
    def check_cancel():
        if cancelled:
            raise Cancelled()
    api.after_write = request_cancel
    with pytest.raises(TransportTransmissionError) as failure:
        transport(api).send_chunks([b"abcdef"], cancel_check=check_cancel)
    assert isinstance(failure.value.__cause__, Cancelled)
    assert bytes(api.data) == b"ab"
    assert ("abort", 10, 42) in api.calls
    assert not any(call[0] == "finish" for call in api.calls)


def test_unavailable_queue_is_not_retried():
    api = FakeSpooler(open_error=OSError("queue unavailable"))
    with pytest.raises(TransportTransmissionError) as failure:
        transport(api).send(b"data")
    assert failure.value.bytes_sent == 0
    assert [call[0] for call in api.calls] == ["open"]


def test_enddoc_error_aborts_and_keeps_byte_accounting():
    api = FakeSpooler(finish_error=True)
    with pytest.raises(TransportTransmissionError) as failure:
        transport(api).send(b"abcd")
    assert failure.value.bytes_sent == 4
    assert ("abort", 10, 42) in api.calls


def test_producer_failure_does_not_append_ff_or_reset():
    api = FakeSpooler()
    def produce(write):
        write(b"\x1b@partial")
        raise ValueError("RIP failed")
    with pytest.raises(TransportTransmissionError):
        transport(api).send_from_producer(produce)
    assert bytes(api.data) == b"\x1b@partial"
    assert ("abort", 10, 42) in api.calls


def test_job_record_failure_aborts_before_first_write():
    api = FakeSpooler()
    def state(*_):
        raise OSError("journal unavailable")
    with pytest.raises(TransportTransmissionError):
        transport(api, job_state=state).send(b"data")
    assert api.data == b""
    assert ("abort", 10, 42) in api.calls


def test_accepted_job_is_not_failed_by_terminal_monitoring_error():
    api = FakeSpooler()
    def state(job, name, status):
        if status == "SPOOLED":
            raise ValueError("monitoring failed")
    assert transport(api, job_state=state).send(b"data").bytes_sent == 4
    assert not any(call[0] == "abort" for call in api.calls)


def test_file_delivery_preserves_hash_and_progress(tmp_path: Path):
    source = tmp_path / "native.prn"
    data = bytes(range(256)) * 32
    source.write_bytes(data)
    api = FakeSpooler(limit=89)
    progress = []
    transport(api).send_file(source, chunk_size=513, progress=lambda *value: progress.append(value))
    assert sha256(api.data).digest() == sha256(data).digest()
    assert progress[-1] == (len(data), len(data))


def test_large_chunks_are_written_with_bounded_native_buffers():
    api = FakeSpooler(limit=65536)
    data = b"\x00\xff" * 150000
    receipt = transport(api).send(data)
    assert bytes(api.data) == data and receipt.bytes_sent == len(data)
    assert max(call[1] for call in api.calls if call[0] == "write") <= 65536


@pytest.mark.parametrize("queue,port,device", [
    ("pyToneRIP P800", "USB004", DEVICE),
    ("P800", "9100", DEVICE),
    ("P800", "USB004", "not-a-device"),
    ("P800", "USB003", DEVICE),
])
def test_rejects_virtual_or_inconsistent_destinations(queue, port, device):
    with pytest.raises(ValueError):
        WindowsPrinterTransport(queue, port, device)


def test_runtime_factory_selects_usb_without_creating_job(tmp_path: Path):
    value = _output_transport({"connection": {
        "kind": "usb", "printer_queue": "Epson physical P800", "usb_port": "USB004",
        "usb_device_id": DEVICE, "port": 9100,
    }}, job_dir=tmp_path)
    assert isinstance(value, WindowsPrinterTransport)
    assert not list(tmp_path.iterdir())
    assert isinstance(_output_transport({"connection": {"kind": "network", "host": "192.0.2.1", "port": 9100}}), RawTcpTransport)


def test_runtime_factory_selects_direct_usb_without_physical_queue(tmp_path: Path):
    value = _output_transport({"connection": {
        "kind": "usb", "usb_device_id": DEVICE, "usb_transport_mode": "usbprint",
    }}, job_dir=tmp_path)
    assert isinstance(value, WindowsUsbInterfaceTransport)
    assert value.device_id == DEVICE
    assert not list(tmp_path.iterdir())


def test_runtime_factory_keeps_physical_queue_as_explicit_fallback(tmp_path: Path):
    value = _output_transport({"connection": {
        "kind": "usb", "printer_queue": "Epson physical P800", "usb_port": "USB004",
        "usb_device_id": DEVICE, "usb_transport_mode": "windows_queue",
    }}, job_dir=tmp_path)
    assert isinstance(value, WindowsPrinterTransport)


def test_p800_golden_prn_is_identical_for_file_tcp_and_windows_queue(tmp_path: Path, monkeypatch):
    from xp750_rip.escp2.backend import EpsonEscp2Backend
    from xp750_rip.printers.xp750_capabilities import default_registry
    from xp750_rip.raster import NamedRasterPlane, PhysicalRasterSet

    model = default_registry().get("SureColorP800")
    mode = model.mode("2880x2880sw")
    blank = bytes(2)
    rows = tuple(bytes((128, 0)) if index == 2 else blank for index in range(8))
    rasters = PhysicalRasterSet((NamedRasterPlane(model.physical_paths[0].identity,
        16, (rows,) + (tuple(blank for _ in rows),) * (mode.bit_depth - 1)),))
    arguments = dict(model=model, mode=mode, medium=model.media[0], page=model.page("A6"),
        input_source=model.input_source("Standard"), rasters=rasters,
        allow_unsupported_ppd_combinations=True)
    backend = EpsonEscp2Backend()
    expected = backend.render(**arguments).data
    assert sha256(expected).hexdigest() == "21ee0536e5caff21a101017035719ef88b236e137721c700a785bc10ead85775"
    target = tmp_path / "p800.prn"
    target.write_bytes(expected)

    def produce(write):
        backend.render_stream(**arguments, write_chunk=write)

    api = FakeSpooler(limit=7)
    receipt = transport(api).send_from_producer(produce)
    class Socket:
        def __init__(self):
            self.data = bytearray()
        def send(self, data):
            count = min(13, len(data))
            self.data.extend(data[:count])
            return count
        def settimeout(self, value):
            pass
        def shutdown(self, value):
            pass
        def close(self):
            pass
    socket = Socket()
    monkeypatch.setattr("xp750_rip.transport.socket.create_connection", lambda *args, **kwargs: socket)
    tcp = RawTcpTransport("192.0.2.1").send_from_producer(produce)
    assert bytes(api.data) == bytes(socket.data) == target.read_bytes() == expected
    assert receipt.bytes_sent == tcp.bytes_sent == 317


@pytest.mark.parametrize("mode,copies", [("online", 1), ("strict_offline", 1), ("strict_offline", 3)])
def test_runtime_usb_stream_and_spool_job_journal(tmp_path: Path, monkeypatch, mode, copies):
    local = tmp_path / "LocalState"
    job_id = "00000000-0000-0000-0000-000000000031"
    folder = local / "jobs" / job_id
    folder.mkdir(parents=True)
    requests = local / "rip-runtime"
    requests.mkdir()
    record = folder / "job-record.json"
    record.write_text(json.dumps({"artifacts": {"configuration": {"file": "configuration.json"}}}), encoding="utf-8")
    configuration = {"calibration": {"enabled": True}, "delivery_mode": mode, "connection": {
        "kind": "usb", "printer_queue": "Epson physical P800", "usb_port": "USB004",
        "usb_device_id": DEVICE, "port": 9100,
    }}
    (folder / "configuration.json").write_text(json.dumps(configuration), encoding="utf-8")
    request = requests / "request.processing.json"
    request.write_text(json.dumps({"schema": "pytone-rip.runtime-request", "version": 2,
        "request_id": "request", "job_id": job_id, "job_record": str(record),
        "printer_key": "usb|" + DEVICE.upper(), "enqueued_utc": "2026-10-12T10:00:00+00:00"}), encoding="utf-8")
    api = FakeSpooler(limit=2)
    data = b"\x1b@header\x00raster\x0c"

    def factory(queue, port, device, **kwargs):
        return WindowsPrinterTransport(queue, port, device, api_factory=lambda: api, **kwargs)

    def render(*args, **kwargs):
        kwargs["output_prn"].write_bytes(data)
        live = kwargs.get("live_transport")
        if live is None:
            return {"status": "PRN_READY_NOT_SENT", "transport_attempted": False, "copies_requested": copies}
        def produce(write):
            write(data[:6])
            assert json.loads((folder / "physical-print-job.json").read_text(encoding="utf-8"))["state"] == "ACTIVE"
            write(data[6:])
            kwargs["send_total_bytes"](len(data))
        receipt = live.send_from_producer(produce, cancel_check=kwargs["cancel_check"], progress=kwargs["send_progress"])
        return {"status": "SENT", "transport": {"bytes_sent": receipt.bytes_sent, "destination": receipt.destination}}

    monkeypatch.setattr("xp750_rip.runtime_host.WindowsPrinterTransport", factory)
    monkeypatch.setattr("xp750_rip.runtime_host.render_psa_job", render)
    assert run_request(request)["status"] == "SENT"
    assert bytes(api.data) == data * copies
    journal = json.loads((folder / "physical-print-job.json").read_text(encoding="utf-8"))
    assert journal["job_id"] == 42 and journal["state"] == "SPOOLED"
    result = json.loads((folder / "transport-result.json").read_text(encoding="utf-8"))
    assert result["delivery_state"] == "SPOOLED"
    assert result["bytes_sent"] == result["total_bytes"] == len(data) * copies
    activity = json.loads((folder / "transport-activity.json").read_text(encoding="utf-8"))
    assert activity["kind"] == "usb" and activity["active"] is False and activity["host"] is None
