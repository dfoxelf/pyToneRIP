# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Bounded producer/sender handoff for immutable native USB print bytes."""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from math import isfinite
from threading import Condition, Event, Lock, Thread, get_native_id
from time import monotonic

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class UsbBufferPolicy:
    capacity_bytes: int = 8 * 1024 * 1024
    prefill_bytes: int = 1024 * 1024
    block_bytes: int = 65536
    coalesce_seconds: float = 0.02
    poll_seconds: float = 0.05
    write_timeout_seconds: float = 300.0

    def __post_init__(self) -> None:
        if any(isinstance(value, bool) or not isinstance(value, int)
               for value in (self.capacity_bytes, self.prefill_bytes, self.block_bytes)):
            raise ValueError("USB byte counts must be integers")
        if not 1 <= self.block_bytes <= 65536:
            raise ValueError("USB block size must be in 1..65536")
        if self.capacity_bytes < self.block_bytes:
            raise ValueError("USB queue must hold at least one block")
        if not 0 <= self.prefill_bytes <= self.capacity_bytes:
            raise ValueError("USB prefill must fit the queue")
        for value in (self.coalesce_seconds, self.poll_seconds, self.write_timeout_seconds):
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not isfinite(value) or value <= 0):
                raise ValueError("USB stream time intervals must be finite and positive")


class BufferedUsbSender:
    """Keep RIP callbacks on their caller; only the sender owns the device handle.

    A monitor requests cancellation of synchronous native I/O. It never closes
    the device underneath WriteFile. Completion always joins both workers.
    """

    def __init__(
        self, *, device_id: str, api_factory: Callable, policy: UsbBufferPolicy,
        connection: Callable[[bool], None], cancel_check: Callable[[], None] | None,
        progress: Callable[[int], None] | None,
        statistics: Callable[[dict], None] | None,
    ) -> None:
        self.device_id = device_id
        self.api_factory = api_factory
        self.policy = policy
        self.connection = connection
        self.cancel_check = cancel_check
        self.progress = progress
        self.statistics = statistics
        self.condition = Condition()
        self.cancel_lock = Lock()
        self.finished = Event()
        self.blocks: deque[bytes] = deque()
        self.pending = bytearray()
        self.buffered = 0
        self.produced = 0
        self.sent = 0
        self.peak = 0
        self.producer_done = False
        self.failure: BaseException | None = None
        self.api = None
        self.sender_id: int | None = None
        self.operation_started: float | None = None
        self.started = monotonic()
        self.pending_since: float | None = None
        self.prefill_seconds = 0.0
        self.producer_wait_seconds = 0.0
        self.underflows = 0
        self.underflow_seconds = 0.0
        self.write_calls = 0
        self.write_seconds = 0.0
        self.max_write_seconds = 0.0
        self.cancel_requests = 0
        self.reported = 0

    def _fail(self, error: BaseException) -> None:
        with self.condition:
            if self.failure is None:
                self.failure = error
            self.blocks.clear()
            self.pending.clear()
            self.pending_since = None
            self.buffered = 0
            self.condition.notify_all()

    def _check_cancel(self) -> None:
        if self.cancel_check is not None:
            with self.cancel_lock:
                self.cancel_check()

    def _report_progress(self) -> None:
        # Called only by the producer/caller thread, including the final join.
        with self.condition:
            sent = self.sent
        if self.progress is not None and sent != self.reported:
            self.reported = sent
            try:
                self.progress(sent)
            except Exception:
                with self.condition:
                    delivered = self._delivered()
                if not delivered:
                    raise
                _LOGGER.debug("USB final progress callback failed after delivery", exc_info=True)

    def _delivered(self) -> bool:
        # Caller holds condition. Once every byte of a completed producer was
        # accepted, a late UI cancel must not encourage printing it again.
        return (self.failure is None and self.producer_done
                and self.produced > 0 and self.sent == self.produced)

    def _enqueue(self, chunk: bytes) -> None:
        if not isinstance(chunk, bytes) or not chunk:
            raise ValueError("live PRN producer must yield non-empty bytes")
        view = memoryview(chunk)
        offset = 0
        while offset < len(view):
            self._check_cancel()
            self._report_progress()
            with self.condition:
                if self.failure is not None:
                    raise self.failure
                if self.buffered == self.policy.capacity_bytes:
                    began = monotonic()
                    self.condition.wait(self.policy.poll_seconds)
                    self.producer_wait_seconds += monotonic() - began
                    continue
                count = min(len(view) - offset,
                            self.policy.capacity_bytes - self.buffered,
                            self.policy.block_bytes - len(self.pending))
                if not self.pending:
                    self.pending_since = monotonic()
                self.pending.extend(view[offset:offset + count])
                offset += count
                self.buffered += count
                self.produced += count
                self.peak = max(self.peak, self.buffered)
                if len(self.pending) == self.policy.block_bytes:
                    self.blocks.append(bytes(self.pending))
                    self.pending.clear()
                    self.pending_since = None
                self.condition.notify_all()

    def _take(self) -> bytes | None:
        empty_since = None
        with self.condition:
            try:
                while self.failure is None:
                    if self.blocks:
                        block = self.blocks.popleft()
                    elif self.pending:
                        delay = self.policy.coalesce_seconds - (monotonic() - self.pending_since)
                        if not self.producer_done and delay > 0:
                            self.condition.wait(min(delay, self.policy.poll_seconds))
                            continue
                        block = bytes(self.pending)
                        self.pending.clear()
                        self.pending_since = None
                    elif self.producer_done:
                        return None
                    else:
                        if empty_since is None:
                            empty_since = monotonic()
                            self.underflows += 1
                        self.condition.wait(self.policy.poll_seconds)
                        continue
                    self.buffered -= len(block)
                    self.condition.notify_all()
                    return block
                return None
            finally:
                if empty_since is not None:
                    self.underflow_seconds += monotonic() - empty_since

    def _send(self) -> None:
        handle = None
        try:
            with self.condition:
                while (self.failure is None and not self.producer_done
                       and (self.buffered == 0 or self.buffered < self.policy.prefill_bytes)):
                    self.condition.wait(self.policy.poll_seconds)
                self.prefill_seconds = monotonic() - self.started
                if self.failure is not None or not self.buffered:
                    return
            api = self.api_factory()
            with self.condition:
                self.api = api
                self.sender_id = get_native_id()
                if self.failure is not None:
                    return
                self.operation_started = monotonic()
            try:
                handle = api.open(self.device_id)
            finally:
                with self.condition:
                    self.operation_started = None
            self.connection(True)
            while (block := self._take()) is not None:
                remaining = memoryview(block)
                while remaining:
                    with self.condition:
                        if self.failure is not None:
                            return
                        began = monotonic()
                        self.operation_started = began
                        self.write_calls += 1
                    try:
                        written = api.write(handle, remaining)
                        if not isinstance(written, int) or written <= 0 or written > len(remaining):
                            raise OSError("WriteFile returned an invalid byte count")
                        with self.condition:
                            # An in-flight call can complete after cancellation.
                            # Its confirmed bytes still belong in the receipt.
                            self.sent += written
                        remaining = remaining[written:]
                    finally:
                        with self.condition:
                            duration = monotonic() - began
                            self.write_seconds += duration
                            self.max_write_seconds = max(self.max_write_seconds, duration)
                            self.operation_started = None
        except BaseException as error:  # noqa: BLE001 - join workers before propagating
            self._fail(error)
        finally:
            if handle is not None:
                try:
                    api.close(handle)
                except BaseException as error:  # noqa: BLE001 - join workers before propagating
                    self._fail(error)
                finally:
                    self.connection(False)
            # The monitor holds this lock while opening the native thread
            # handle. The sender cannot exit and have its ID reused then.
            with self.condition:
                self.finished.set()

    def _monitor(self) -> None:
        while not self.finished.wait(self.policy.poll_seconds):
            with self.condition:
                if self._delivered():
                    return
            try:
                self._check_cancel()
            except BaseException as error:  # noqa: BLE001 - join workers before propagating
                with self.condition:
                    if not self._delivered():
                        self._fail(error)
            with self.condition:
                began = self.operation_started
                if (self.failure is None and began is not None
                        and monotonic() - began >= self.policy.write_timeout_seconds):
                    self._fail(TimeoutError("USB printer stopped accepting PRN data"))
                if self.finished.is_set():
                    return
                if self.failure is not None and self.api is not None and self.sender_id is not None:
                    # Repeat to cover cancellation between the pre-write check
                    # and entry into the synchronous kernel call. Keep the
                    # sender alive until OpenThread/CancelSynchronousIo return.
                    cancel = getattr(self.api, "cancel", None)
                    if cancel is not None:
                        try:
                            self.cancel_requests += 1
                            cancel(self.sender_id)
                        except Exception:
                            _LOGGER.debug("USB synchronous I/O cancellation failed", exc_info=True)

    def _statistics(self) -> dict:
        return {
            "schema": "pytone-rip.usb-streaming", "version": 1,
            "completed": self.failure is None,
            "queue_capacity_bytes": self.policy.capacity_bytes,
            "prefill_target_bytes": self.policy.prefill_bytes,
            "block_bytes": self.policy.block_bytes,
            "produced_bytes": self.produced, "bytes_sent": self.sent,
            "queue_peak_bytes": self.peak,
            "prefill_seconds": round(self.prefill_seconds, 6),
            "producer_wait_seconds": round(self.producer_wait_seconds, 6),
            "queue_empty_waits": self.underflows,
            "queue_empty_seconds": round(self.underflow_seconds, 6),
            "native_write_calls": self.write_calls,
            "native_write_seconds": round(self.write_seconds, 6),
            "max_native_write_seconds": round(self.max_write_seconds, 6),
            "native_cancel_requests": self.cancel_requests,
            "elapsed_seconds": round(monotonic() - self.started, 6),
        }

    def run(self, producer: Callable[[Callable[[bytes], None]], None]) -> int:
        sender = Thread(target=self._send, name="pyToneRIP USB sender")
        monitor = Thread(target=self._monitor, name="pyToneRIP USB monitor")
        sender_started = monitor_started = False
        try:
            self._check_cancel()
            sender.start()
            sender_started = True
            monitor.start()
            monitor_started = True
            producer(self._enqueue)
            with self.condition:
                if not self.produced:
                    raise ValueError("live PRN producer emitted no data")
        except BaseException as error:  # noqa: BLE001 - join workers before propagating
            self._fail(error)
        finally:
            with self.condition:
                self.producer_done = True
                self.condition.notify_all()
            if sender_started:
                while True:
                    try:
                        if self.finished.wait(self.policy.poll_seconds):
                            break
                        self._report_progress()
                    except BaseException as error:  # noqa: BLE001 - join workers before propagating
                        self._fail(error)
                sender.join()
            if monitor_started:
                monitor.join()
            try:
                self._report_progress()
            except BaseException as error:  # noqa: BLE001 - join workers before propagating
                self._fail(error)
            if self.statistics is not None:
                try:
                    self.statistics(self._statistics())
                except Exception:
                    _LOGGER.debug("USB statistics callback failed", exc_info=True)
        if self.failure is not None:
            raise self.failure
        return self.sent
