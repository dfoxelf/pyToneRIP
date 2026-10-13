# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Parser for key/value option strings observed in the real QTR runtime log."""

from __future__ import annotations

import shlex
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class QtrOption:
    raw_token: str
    original_key: str
    normalized_key: str
    raw_value: str


@dataclass(frozen=True, slots=True)
class QtrOptionSet:
    source: str
    options: tuple[QtrOption, ...]

    def values(self, key: str) -> tuple[str, ...]:
        normalized = key.casefold()
        return tuple(
            option.raw_value for option in self.options if option.normalized_key == normalized
        )

    def value(self, key: str) -> str | None:
        values = self.values(key)
        return values[-1] if values else None

    @property
    def unknown_keys(self) -> tuple[str, ...]:
        observed = {
            "ppd",
            "printer",
            "mediatype",
            "pagesize",
            "position",
            "scale-percent",
            "landscape",
            "resolution",
            "colormodel",
            "ripspeed",
            "ripcurve1",
            "ripcurve2",
            "ripcurve3",
            "ripblend1h",
            "ripblend1m",
            "ripblend1s",
            "ripblend2h",
            "ripblend2m",
            "ripblend2s",
            "ripblend3h",
            "ripblend3m",
            "ripblend3s",
            "stpdither",
            "ripgamma",
            "riplimit",
        }
        return tuple(
            option.original_key for option in self.options if option.normalized_key not in observed
        )


def parse_qtr_options(source: str) -> QtrOptionSet:
    """Parse a QTR command option string, retaining order, spelling and values."""

    tokens = shlex.split(source, posix=True)
    options: list[QtrOption] = []
    for token in tokens:
        if "=" not in token:
            raise ValueError(f"QTR option token must be KEY=value, got {token!r}")
        key, value = token.split("=", 1)
        if not key:
            raise ValueError(f"QTR option token has an empty key: {token!r}")
        options.append(QtrOption(token, key, key.casefold(), value))
    return QtrOptionSet(source, tuple(options))
