# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Packaged Phase 12.2 host: one strict LocalState request to one offline PRN."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Lock, Thread
from time import perf_counter, sleep
from typing import Any, Callable

from xp750_rip.performance import PerformanceTrace
from xp750_rip.product_cli import render_psa_job
from xp750_rip.transport import RawTcpTransport, TransportTransmissionError

REQUEST_SCHEMA = "pytone-rip.runtime-request"
PROGRESS_SCHEMA = "pytone-rip.job-progress"
SCHEMA_VERSION = 1
REQUEST_SCHEMA_VERSION = 2
CANCELLATION_FILE_NAME = "cancel.requested"


class JobCancelledError(RuntimeError):
    """Raised when Windows cancels the specific job being rendered."""


def _raise_if_cancelled(job_dir: Path) -> None:
    if (job_dir / CANCELLATION_FILE_NAME).is_file():
        raise JobCancelledError("Windows print job was canceled.")


def _atomic_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    for attempt in range(6):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            if attempt == 5:
                raise
            # The Manager may have progress.json open for a very short read.
            sleep(0.04 * (attempt + 1))


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


class JobProgress:
    def __init__(self, path: Path, job_id: str) -> None:
        self.path = path
        self.job_id = job_id
        self.percent = 0

    def write(
        self,
        percent: int,
        stage: str,
        message: str,
        *,
        status: str = "RUNNING",
        terminal: bool = False,
    ) -> None:
        self.percent = max(self.percent, min(100, percent))
        updated_utc = datetime.now(UTC).isoformat()
        value = {
            "schema": PROGRESS_SCHEMA,
            "version": SCHEMA_VERSION,
            "job_id": self.job_id,
            "status": status,
            "stage": stage,
            "percent": self.percent,
            "message": message,
            "updated_utc": updated_utc,
            "terminal": terminal,
        }
        self._append_history(updated_utc, stage, message)
        _atomic_json(self.path, value)

    def record_stage(self, stage: str, message: str = "") -> None:
        """Append a detailed trace boundary without replacing UI progress."""

        self._append_history(datetime.now(UTC).isoformat(), stage, message)

    def _append_history(self, updated_utc: str, stage: str, message: str) -> None:
        """Preserve every stage for the optional Manager log."""

        safe_message = message.replace("\r", " ").replace("\n", " ")
        try:
            with (self.path.parent / "progress-history.log").open(
                "a", encoding="utf-8"
            ) as history:
                history.write(
                    f"{updated_utc} | {self.percent}% | {stage} | {safe_message}\n"
                )
        except OSError:
            # This log is observational. Printing must not fail if the Manager
            # briefly locks the file while reading newly appended stages.
            pass


class RenderProgressAdapter:
    """Turn real PerformanceTrace boundaries into monotonic coarse UI progress."""

    def __init__(self, progress: JobProgress, job_dir: Path) -> None:
        self.progress = progress
        self.job_dir = job_dir
        self.next_percent = 46

    def __call__(self, label: str) -> None:
        if label == "TOTAL":
            # This is bookkeeping after the job outcome is already known.
            # A late cancellation marker cannot undo completed transmission.
            self.progress.record_stage(label)
            return
        _raise_if_cancelled(self.job_dir)
        if not (label.endswith(" START") or "logical contone plane" in label):
            self.progress.record_stage(label)
            return
        percent = min(self.next_percent, 88)
        self.next_percent = min(self.next_percent + 2, 88)
        stage = label.removesuffix(" START")
        self.progress.write(percent, "PYTHON_RIP", stage)


def _load_request(request_path: Path) -> tuple[dict[str, Any], Path, Path]:
    resolved_request = request_path.resolve(strict=True)
    if resolved_request.parent.name.casefold() != "rip-runtime":
        raise ValueError("runtime request must be inside LocalState/rip-runtime")
    local_state = resolved_request.parent.parent.resolve(strict=True)
    request = json.loads(resolved_request.read_text(encoding="utf-8"))
    if not isinstance(request, dict):
        raise TypeError("runtime request must be a JSON object")
    required = {
        "schema",
        "version",
        "request_id",
        "job_id",
        "job_record",
        "printer_key",
        "enqueued_utc",
    }
    if set(request) != required:
        raise ValueError("runtime request has unknown or missing fields")
    if (
        request["schema"] != REQUEST_SCHEMA
        or request["version"] != REQUEST_SCHEMA_VERSION
    ):
        raise ValueError("unsupported runtime request schema/version")
    for key in (
        "request_id",
        "job_id",
        "job_record",
        "printer_key",
        "enqueued_utc",
    ):
        if not isinstance(request[key], str) or not request[key].strip():
            raise ValueError(f"runtime request {key} must be a non-empty string")
    try:
        enqueued = datetime.fromisoformat(request["enqueued_utc"])
    except ValueError as error:
        raise ValueError("runtime request enqueued_utc must be ISO-8601") from error
    if enqueued.tzinfo is None:
        raise ValueError("runtime request enqueued_utc must include a timezone")
    record = Path(request["job_record"]).resolve(strict=True)
    local_record = (
        local_state / "jobs" / request["job_id"] / "job-record.json"
    ).resolve()
    shared_record = (
        Path(os.environ.get("LOCALAPPDATA", ""))
        / "Publishers"
        / "qg4mjaqw0qctt"
        / "pyToneRIP.Shared"
        / "jobs"
        / request["job_id"]
        / "job-record.json"
    ).resolve()
    if record not in {local_record, shared_record}:
        raise ValueError(
            "runtime job record is outside LocalState/shared publisher jobs"
        )
    job_dir = record.parent
    return request, record, job_dir


def _load_job_configuration(record: Path, job_dir: Path) -> dict[str, Any]:
    job = json.loads(record.read_text(encoding="utf-8"))
    config_name = job["artifacts"]["configuration"]["file"]
    configuration_path = (job_dir / config_name).resolve(strict=True)
    if not _is_within(configuration_path, job_dir):
        raise ValueError("configuration artifact is outside the job directory")
    value = json.loads(configuration_path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError("job configuration must be a JSON object")
    return value


def _validate_external_curve_sources(configuration: dict[str, Any]) -> None:
    calibration = configuration.get("calibration")
    if isinstance(calibration, dict) and calibration.get("enabled") is True:
        return
    for curve in configuration["profiles"]["curves"]:
        curve_path = Path(curve).resolve(strict=True)
        if not curve_path.is_file() or curve_path.suffix.casefold() != ".quad":
            raise ValueError("RIP curve must be an existing external .quad file")


def _network_transport(
    configuration: dict[str, Any],
    connection_state: Callable[[bool], None] | None = None,
) -> RawTcpTransport:
    connection = configuration.get("connection")
    if not isinstance(connection, dict):
        raise ValueError("printer network connection is not configured")
    if connection.get("kind") != "network":
        raise ValueError("automatic physical transport currently supports only network")
    host = connection.get("host")
    if not isinstance(host, str) or not host.strip():
        raise ValueError("printer IPv4 address is not configured")
    try:
        normalized_host = str(ipaddress.IPv4Address(host.strip()))
    except ipaddress.AddressValueError as error:
        raise ValueError("printer host must be a valid IPv4 address") from error
    port = connection.get("port", 9100)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("printer TCP port must be in range 1..65535")
    return RawTcpTransport(normalized_host, port, connection_state=connection_state)


def _write_transport_result(
    path: Path,
    *,
    job_id: str,
    status: str,
    destination: str,
    bytes_sent: int,
    total_bytes: int,
    duration_seconds: float,
    error: str | None = None,
    abort_attempted: bool = False,
    abort_succeeded: bool = False,
) -> dict[str, object]:
    result: dict[str, object] = {
        "schema": "pytone-rip.transport-result",
        "version": 1,
        "job_id": job_id,
        "status": status,
        "destination": destination,
        "bytes_sent": bytes_sent,
        "total_bytes": total_bytes,
        "duration_seconds": round(duration_seconds, 6),
        "completed_utc": datetime.now(UTC).isoformat(),
        "abort_attempted": abort_attempted,
        "abort_succeeded": abort_succeeded,
    }
    if error is not None:
        result["error"] = error
    _atomic_json(path, result)
    return result


def run_request(request_path: Path) -> dict[str, object]:
    request, record, job_dir = _load_request(request_path)
    progress = JobProgress(job_dir / "progress.json", str(request["job_id"]))
    transport: RawTcpTransport | None = None
    activity_lock = Lock()
    heartbeat_stop: Event | None = None

    def write_transport_activity(active: bool) -> None:
        if transport is None:
            return
        _atomic_json(
            job_dir / "transport-activity.json",
            {
                "schema": "pytone-rip.transport-activity",
                "version": 1,
                "job_id": str(request["job_id"]),
                "host": transport.host,
                "port": transport.port,
                "active": active,
                "updated_utc": datetime.now(UTC).isoformat(),
            },
        )

    def record_transport_connection(active: bool) -> None:
        nonlocal heartbeat_stop
        if active:
            stop = Event()
            with activity_lock:
                if heartbeat_stop is not None:
                    heartbeat_stop.set()
                heartbeat_stop = stop
                write_transport_activity(True)

            def heartbeat() -> None:
                while not stop.wait(30):
                    with activity_lock:
                        if stop.is_set():
                            return
                        try:
                            write_transport_activity(True)
                        except OSError:
                            # Monitoring may fail without interrupting printing.
                            pass

            Thread(target=heartbeat, daemon=True).start()
        else:
            with activity_lock:
                if heartbeat_stop is not None:
                    heartbeat_stop.set()
                    heartbeat_stop = None
                write_transport_activity(False)
    try:
        _raise_if_cancelled(job_dir)
        progress.write(32, "RUNTIME_START", "Packaged Python runtime started")
        configuration = _load_job_configuration(record, job_dir)
        _validate_external_curve_sources(configuration)
        # Old saved configurations retain their validated offline behavior.
        delivery_mode = configuration.get("delivery_mode", "strict_offline")
        if delivery_mode not in {"online", "strict_offline"}:
            raise ValueError("delivery_mode must be online or strict_offline")
        _raise_if_cancelled(job_dir)
        progress.write(35, "VALIDATION", "Job request and external curves validated")
        trace = PerformanceTrace(
            job_dir / "performance.log",
            event_callback=RenderProgressAdapter(progress, job_dir),
        )
        status = "ERROR"
        try:
            progress.write(40, "TIFF_NORMALIZE", "Preparing Windows raster for RIP")
            if delivery_mode == "online":
                transport = _network_transport(
                    configuration, record_transport_connection
                )
                started = perf_counter()
                last_reported = 0
                last_sent = 0
                live_total: int | None = None

                def write_live_progress() -> None:
                    total = str(live_total) if live_total is not None else "…"
                    progress.write(
                        min(95, progress.percent + 1),
                        "RIP_STREAMING",
                        f"Rendered and sent {last_sent} / {total} PRN bytes",
                    )

                def report_live(bytes_sent: int) -> None:
                    nonlocal last_reported, last_sent
                    _raise_if_cancelled(job_dir)
                    last_sent = bytes_sent
                    if bytes_sent - last_reported >= 1024 * 1024:
                        last_reported = bytes_sent
                        write_live_progress()

                def report_live_total(total_bytes: int) -> None:
                    nonlocal live_total
                    live_total = total_bytes
                    write_live_progress()

                try:
                    payload = render_psa_job(
                        record,
                        output_prn=job_dir / "output.prn",
                        normalized_tiff=job_dir / "python-input-grayscale.tiff",
                        manifest=job_dir / "render-manifest.json",
                        result_path=job_dir / "render-result.json",
                        performance_trace=trace,
                        live_transport=transport,
                        cancel_check=lambda: _raise_if_cancelled(job_dir),
                        send_progress=report_live,
                        send_total_bytes=report_live_total,
                    )
                except TransportTransmissionError as error:
                    cancelled = isinstance(error.__cause__, JobCancelledError)
                    _write_transport_result(
                        job_dir / "transport-result.json",
                        job_id=str(request["job_id"]),
                        status="CANCELLED" if cancelled else "FAILED",
                        destination=error.destination,
                        bytes_sent=error.bytes_sent,
                        total_bytes=live_total or 0,
                        duration_seconds=perf_counter() - started,
                        error=str(error),
                        abort_attempted=error.abort_attempted,
                        abort_succeeded=error.abort_succeeded,
                    )
                    if cancelled:
                        raise error.__cause__ from error
                    raise
                # Once the complete native job has been sent, a late cancel
                # cannot retract it and must not turn a successful send into
                # a misleading CANCELLED result.
                sent = int(payload["transport"]["bytes_sent"])
                _write_transport_result(
                    job_dir / "transport-result.json",
                    job_id=str(request["job_id"]),
                    status="SENT",
                    destination=str(payload["transport"]["destination"]),
                    bytes_sent=sent,
                    total_bytes=sent,
                    duration_seconds=perf_counter() - started,
                )
                progress.write(
                    100, "SENT", f"Rendered and sent {sent} / {sent} PRN bytes",
                    status="SENT", terminal=True,
                )
                status = "PASS"
                return payload
            payload = render_psa_job(
                record,
                output_prn=job_dir / "output.prn",
                normalized_tiff=job_dir / "python-input-grayscale.tiff",
                manifest=job_dir / "render-manifest.json",
                result_path=job_dir / "render-result.json",
                performance_trace=trace,
            )
            _raise_if_cancelled(job_dir)
            status = "PASS"
        finally:
            trace.finish(status=status)
            trace.close()
        if payload.get("status") != "PRN_READY_NOT_SENT":
            raise ValueError("runtime did not produce PRN_READY_NOT_SENT")
        if payload.get("transport_attempted") is not False:
            raise ValueError("offline runtime reported a transport attempt")
        output_prn = (job_dir / "output.prn").resolve(strict=True)
        if not _is_within(output_prn, job_dir):
            raise ValueError("rendered PRN is outside the job directory")
        transport = _network_transport(configuration, record_transport_connection)
        _raise_if_cancelled(job_dir)
        copies = payload.get("copies_requested", 1)
        if isinstance(copies, bool) or not isinstance(copies, int) or not 1 <= copies <= 99:
            raise ValueError("copies_requested must be in 1..99")
        total_bytes = output_prn.stat().st_size * copies
        started = perf_counter()
        progress.write(
            90,
            "TRANSPORT_CONNECT",
            f"Connecting to {transport.host}:{transport.port}",
        )

        def report_send(bytes_sent: int, byte_count: int) -> None:
            _raise_if_cancelled(job_dir)
            percent = 91 + min(8, int(bytes_sent * 8 / max(1, byte_count)))
            progress.write(
                percent,
                "TRANSPORT_SEND",
                f"Sent {bytes_sent} / {byte_count} PRN bytes",
            )

        try:
            if copies == 1:
                receipt = transport.send_file(
                    output_prn,
                    progress=report_send,
                    cancel_check=lambda: _raise_if_cancelled(job_dir),
                )
            else:
                def native_copies():
                    for _ in range(copies):
                        with output_prn.open("rb") as source:
                            while chunk := source.read(1024 * 1024):
                                yield chunk

                receipt = transport.send_chunks(
                    native_copies(),
                    progress=lambda sent: report_send(sent, total_bytes),
                    cancel_check=lambda: _raise_if_cancelled(job_dir),
                )
        except TransportTransmissionError as error:
            cancelled = isinstance(error.__cause__, JobCancelledError)
            _write_transport_result(
                job_dir / "transport-result.json",
                job_id=str(request["job_id"]),
                status="CANCELLED" if cancelled else "FAILED",
                destination=error.destination,
                bytes_sent=error.bytes_sent,
                total_bytes=total_bytes,
                duration_seconds=perf_counter() - started,
                error=str(error),
                abort_attempted=error.abort_attempted,
                abort_succeeded=error.abort_succeeded,
            )
            payload["status"] = "CANCELLED" if cancelled else "TRANSPORT_FAILED"
            payload["transport_attempted"] = True
            _atomic_json(job_dir / "render-result.json", payload)
            if cancelled:
                raise error.__cause__ from error
            raise
        transport_result = _write_transport_result(
            job_dir / "transport-result.json",
            job_id=str(request["job_id"]),
            status="SENT",
            destination=receipt.destination,
            bytes_sent=receipt.bytes_sent,
            total_bytes=total_bytes,
            duration_seconds=perf_counter() - started,
        )
        payload["status"] = "SENT"
        payload["transport_attempted"] = True
        payload["transport"] = transport_result
        _atomic_json(job_dir / "render-result.json", payload)
        progress.write(
            100,
            "SENT",
            f"Sent {receipt.bytes_sent} / {total_bytes} PRN bytes to {receipt.destination}",
            status="SENT",
            terminal=True,
        )
        return payload
    except JobCancelledError as error:
        progress.write(
            100,
            "CANCELLED",
            str(error),
            status="CANCELLED",
            terminal=True,
        )
        raise
    except Exception as error:
        progress.write(
            100,
            "FAILED",
            f"{type(error).__name__}: {error}",
            status="FAILED",
            terminal=True,
        )
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pyToneRIP.Runtime")
    command = parser.add_mutually_exclusive_group(required=True)
    command.add_argument("--request", type=Path)
    command.add_argument("--maintenance-request", type=Path)
    command.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.self_test:
            from xp750_rip._cython_kernels import self_test

            self_test()
        elif args.maintenance_request is not None:
            from xp750_rip.maintenance import execute_maintenance_file

            request_path = args.maintenance_request.resolve(strict=True)
            if request_path.parent.name.casefold() != "maintenance":
                raise ValueError("maintenance request must be inside LocalState/maintenance")
            execute_maintenance_file(request_path)
        else:
            run_request(args.request)
    except Exception as error:  # noqa: BLE001 - process boundary must return a broker error
        if args.request is not None:
            try:
                request_path = args.request.resolve()
                error_path = request_path.parent / (request_path.stem + ".error.txt")
                error_path.write_text(f"{type(error).__name__}: {error}\n", encoding="utf-8")
            except OSError:
                return 1
        elif args.maintenance_request is not None:
            try:
                request_path = args.maintenance_request.resolve()
                error_path = request_path.with_suffix(".error.txt")
                error_path.write_text(f"{type(error).__name__}: {error}\n", encoding="utf-8")
            except OSError:
                return 1
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
