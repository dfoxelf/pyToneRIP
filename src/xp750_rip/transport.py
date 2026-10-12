# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Transport boundary that receives only already validated immutable bytes."""

from __future__ import annotations

import socket
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from typing import Protocol

# Epson Raw TCP can stop accepting data temporarily while its small input
# buffer is consumed by the print engine. Allow a five-minute pause before
# treating that pause as a broken job.
DEFAULT_RAW_TCP_TIMEOUT_SECONDS = 300.0


def _abort_native_job(connection: socket.socket, timeout_seconds: float) -> bool:
    """Try to reset the ESC/P2 parser on the still-open transport connection."""

    from xp750_rip.escp2 import commands

    try:
        connection.settimeout(min(1.0, timeout_seconds))
        connection.sendall(commands.reset() + commands.reset())
    except OSError:
        return False
    return True


@dataclass(frozen=True, slots=True)
class TransmissionReceipt:
    bytes_sent: int
    destination: str


class TransportTransmissionError(OSError):
    """A non-retryable send failure with the known completed byte count."""

    def __init__(
        self,
        message: str,
        *,
        bytes_sent: int,
        destination: str,
        abort_attempted: bool = False,
        abort_succeeded: bool = False,
    ) -> None:
        super().__init__(message)
        self.bytes_sent = bytes_sent
        self.destination = destination
        self.abort_attempted = abort_attempted
        self.abort_succeeded = abort_succeeded


class Transport(Protocol):
    def send(self, data: bytes) -> TransmissionReceipt: ...


class StreamingTransport(Protocol):
    def send_from_producer(
        self, producer: Callable[[Callable[[bytes], None]], None], *,
        cancel_check: Callable[[], None] | None = None,
        progress: Callable[[int], None] | None = None,
    ) -> TransmissionReceipt: ...

    def send_file(
        self, path: Path, *, chunk_size: int = 1024 * 1024,
        progress: Callable[[int, int], None] | None = None,
        cancel_check: Callable[[], None] | None = None,
    ) -> TransmissionReceipt: ...

    def send_chunks(
        self, chunks: Iterable[bytes], *, cancel_check: Callable[[], None] | None = None,
        progress: Callable[[int], None] | None = None,
    ) -> TransmissionReceipt: ...


@dataclass(frozen=True, slots=True)
class RawTcpTransport:
    host: str
    port: int = 9100
    timeout_seconds: float = DEFAULT_RAW_TCP_TIMEOUT_SECONDS
    connection_state: Callable[[bool], None] | None = field(
        default=None, compare=False, repr=False
    )

    def __post_init__(self) -> None:
        if not self.host.strip():
            raise ValueError("RawTcpTransport host не может быть пустым")
        if not 1 <= self.port <= 65535:
            raise ValueError("RawTcpTransport port должен быть в диапазоне 1..65535")
        if self.timeout_seconds <= 0:
            raise ValueError("RawTcpTransport timeout должен быть положительным")

    def send(self, data: bytes) -> TransmissionReceipt:
        if not isinstance(data, bytes):
            raise TypeError("RawTcpTransport принимает только bytes")
        try:
            with socket.create_connection(
                (self.host, self.port), timeout=self.timeout_seconds
            ) as connection:
                self._report_connection(True)
                connection.sendall(data)
                connection.shutdown(socket.SHUT_WR)
        finally:
            self._report_connection(False)
        return TransmissionReceipt(len(data), f"raw_tcp://{self.host}:{self.port}")

    def _report_connection(self, active: bool) -> None:
        if self.connection_state is not None:
            try:
                self.connection_state(active)
            except Exception:
                # Status reporting must not interrupt a physical print job.
                pass

    def send_chunks(
        self,
        chunks: Iterable[bytes],
        *,
        cancel_check: Callable[[], None] | None = None,
        progress: Callable[[int], None] | None = None,
    ) -> TransmissionReceipt:
        def consume(write: Callable[[bytes], None]) -> None:
            for chunk in chunks:
                write(chunk)

        return self.send_from_producer(
            consume,
            cancel_check=cancel_check,
            progress=progress,
        )

    def send_from_producer(
        self,
        producer: Callable[[Callable[[bytes], None]], None],
        *,
        cancel_check: Callable[[], None] | None = None,
        progress: Callable[[int], None] | None = None,
    ) -> TransmissionReceipt:
        """Consume a live PRN producer with backpressure and best-effort abort.

        The producer must emit a complete native job, including its normal
        trailer, only on success.  A producer or send failure never causes a
        form feed.  ESC @ is attempted on the same connection before closing;
        this resets the command parser but cannot retract ink already printed.
        """

        destination = f"raw_tcp://{self.host}:{self.port}"
        sent = 0
        abort_attempted = False
        abort_succeeded = False
        connection: socket.socket | None = None
        try:
            if cancel_check is not None:
                cancel_check()

            def write(chunk: bytes) -> None:
                nonlocal sent, connection
                if cancel_check is not None:
                    cancel_check()
                if not isinstance(chunk, bytes) or not chunk:
                    raise ValueError("live PRN producer must yield non-empty bytes")
                if connection is None:
                    connection = socket.create_connection(
                        (self.host, self.port), timeout=self.timeout_seconds
                    )
                    connection.settimeout(min(1.0, self.timeout_seconds))
                    self._report_connection(True)
                remaining = memoryview(chunk)
                last_progress = monotonic()
                while remaining:
                    if cancel_check is not None:
                        cancel_check()
                    try:
                        written = connection.send(remaining)
                    except TimeoutError:
                        if monotonic() - last_progress >= self.timeout_seconds:
                            raise TimeoutError("printer stopped accepting PRN data")
                        continue
                    if written <= 0:
                        raise ConnectionError("printer closed the RAW TCP connection")
                    sent += written
                    remaining = remaining[written:]
                    last_progress = monotonic()
                if progress is not None:
                    progress(sent)
            producer(write)
            if sent == 0 or connection is None:
                raise ValueError("live PRN producer emitted no data")
            connection.shutdown(socket.SHUT_WR)
            return TransmissionReceipt(sent, destination)
        except Exception as error:
            if connection is not None and sent:
                abort_attempted = True
                # Never send the normal FF for an incomplete job.
                abort_succeeded = _abort_native_job(connection, self.timeout_seconds)
            if isinstance(error, TransportTransmissionError):
                raise
            raise TransportTransmissionError(
                f"RAW TCP live job failed after {sent} bytes: {error}",
                bytes_sent=sent,
                destination=destination,
                abort_attempted=abort_attempted,
                abort_succeeded=abort_succeeded,
            ) from error
        finally:
            if connection is not None:
                connection.close()
                self._report_connection(False)

    def send_file(
        self,
        path: Path,
        *,
        chunk_size: int = 1024 * 1024,
        progress: Callable[[int, int], None] | None = None,
        cancel_check: Callable[[], None] | None = None,
    ) -> TransmissionReceipt:
        """Stream an immutable PRN without loading the whole job into memory."""

        source = Path(path).resolve(strict=True)
        if not source.is_file():
            raise ValueError("RawTcpTransport ожидает существующий PRN-файл")
        if chunk_size <= 0:
            raise ValueError("RawTcpTransport chunk_size должен быть положительным")
        total = source.stat().st_size
        if total <= 0:
            raise ValueError("RawTcpTransport не отправляет пустой PRN-файл")
        destination = f"raw_tcp://{self.host}:{self.port}"
        sent = 0
        began_send = False
        connection: socket.socket | None = None
        try:
            with source.open("rb") as stream:
                if cancel_check is not None:
                    cancel_check()
                connection = socket.create_connection(
                    (self.host, self.port), timeout=self.timeout_seconds
                )
                self._report_connection(True)
                if cancel_check is not None:
                    connection.settimeout(min(1.0, self.timeout_seconds))
                while True:
                    if cancel_check is not None:
                        cancel_check()
                    chunk = stream.read(chunk_size)
                    if not chunk:
                        break
                    began_send = True
                    if cancel_check is None:
                        connection.sendall(chunk)
                        sent += len(chunk)
                    else:
                        remaining = memoryview(chunk)
                        last_progress = monotonic()
                        while remaining:
                            cancel_check()
                            try:
                                written = connection.send(remaining)
                            except TimeoutError:
                                if monotonic() - last_progress >= self.timeout_seconds:
                                    raise TimeoutError("printer stopped accepting PRN data")
                                continue
                            if written <= 0:
                                raise ConnectionError("printer closed the RAW TCP connection")
                            sent += written
                            remaining = remaining[written:]
                            last_progress = monotonic()
                    if progress is not None:
                        progress(sent, total)
                connection.shutdown(socket.SHUT_WR)
        except Exception as error:
            abort_attempted = connection is not None and (sent > 0 or began_send)
            abort_succeeded = (
                _abort_native_job(connection, self.timeout_seconds)
                if abort_attempted and connection is not None
                else False
            )
            raise TransportTransmissionError(
                f"RAW TCP send failed after {sent} of {total} bytes: {error}",
                bytes_sent=sent,
                destination=destination,
                abort_attempted=abort_attempted,
                abort_succeeded=abort_succeeded,
            ) from error
        finally:
            if connection is not None:
                connection.close()
                self._report_connection(False)
        return TransmissionReceipt(sent, destination)
