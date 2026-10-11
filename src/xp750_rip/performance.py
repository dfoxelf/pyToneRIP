# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Опциональная малошумная трассировка производительности production pipeline."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import IO


def _format_value(value: object) -> str:
    if isinstance(value, float):
        return f"{value:.6f}"
    text = str(value)
    return repr(text) if any(character.isspace() for character in text) else text


class PerformanceTrace:
    """Пишет только границы крупных стадий и сразу сбрасывает строки на диск."""

    def __init__(
        self,
        path: Path,
        *,
        event_callback: Callable[[str], None] | None = None,
    ) -> None:
        self.path = path
        self._event_callback = event_callback
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._stream: IO[str] = self.path.open("w", encoding="utf-8", newline="\n")
        self._job_started = perf_counter()
        self.event("START job")

    def _timestamp(self) -> str:
        return datetime.now().astimezone().strftime("%H:%M:%S.%f")[:-3]

    def event(self, label: str, **fields: object) -> None:
        suffix = "".join(f"  {key}={_format_value(value)}" for key, value in fields.items())
        self._stream.write(f"{self._timestamp()}  {label}{suffix}\n")
        self._stream.flush()
        if self._event_callback is not None:
            self._event_callback(label)

    def record_duration(self, label: str, duration: float, **fields: object) -> None:
        self.event(f"{label} END", duration=f"{duration:.6f}s", **fields)

    @contextmanager
    def stage(self, label: str, **fields: object) -> Iterator[None]:
        self.event(f"{label} START", **fields)
        started = perf_counter()
        try:
            yield
        except BaseException as error:
            self.event(
                f"{label} ERROR",
                duration=f"{perf_counter() - started:.6f}s",
                error=type(error).__name__,
            )
            raise
        else:
            self.record_duration(label, perf_counter() - started)

    def finish(self, *, status: str) -> None:
        self.event(
            "TOTAL",
            duration=f"{perf_counter() - self._job_started:.6f}s",
            status=status,
        )

    def close(self) -> None:
        self._stream.close()
