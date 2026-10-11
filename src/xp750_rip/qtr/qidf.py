# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Preservation-first parser for QTR ``.qidf`` / macOS ``.txt`` descriptors."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

_INTEGER_RE = re.compile(r"^[0-9]+$")
_REAL_RE = re.compile(r"^[0-9]+(?:[.,][0-9]+)?$")
_PART_INK_RE = re.compile(r"^(GRAY|TONER|TONER_2)_INK_([0-9]+)$")
_PART_VALUE_RE = re.compile(r"^(GRAY|TONER|TONER_2)_VAL_([0-9]+)$")
_LIMIT_RE = re.compile(r"^LIMIT_(.+)$")
_COPY_RE = re.compile(r"^COPY_CURVE_(.+)$")

_INTEGER_KEYS = {
    "N_OF_INKS",
    "N_OF_GRAY_PARTS",
    "N_OF_TONER_PARTS",
    "N_OF_TONER_2_PARTS",
    "N_OF_UNUSED",
}
_REAL_KEYS = {
    "DEFAULT_INK_LIMIT",
    "BOOST_K",
    "GRAY_HIGHLIGHT",
    "GRAY_SHADOW",
    "GRAY_OVERLAP",
    "GRAY_GAMMA",
    "TONER_HIGHLIGHT",
    "TONER_SHADOW",
    "TONER_GAMMA",
    "TONER_2_HIGHLIGHT",
    "TONER_2_SHADOW",
    "TONER_2_GAMMA",
}
_BOOL_KEYS = {"GRAPH_CURVE", "CALIBRATION", "UC_NEUTRALIZER", "UC_NEUTRALIZER2"}
_SEQUENCE_KEYS = {"LINEARIZE"}


@dataclass(frozen=True, slots=True)
class QidfEntry:
    line_number: int
    raw: str
    kind: str
    original_key: str | None = None
    normalized_key: str | None = None
    raw_value: str | None = None


@dataclass(frozen=True, slots=True)
class QidfPartitionPart:
    ordinal: int
    ink_code: str
    relative_density: Decimal


@dataclass(frozen=True, slots=True)
class QidfPartition:
    name: str
    parts: tuple[QidfPartitionPart, ...]
    declared_part_count: int | None
    highlight: Decimal | None
    shadow: Decimal | None
    gamma: Decimal | None
    overlap: Decimal | None
    correction_raw: str | None


@dataclass(frozen=True, slots=True)
class QidfDocument:
    """QIDF source plus typed access to documented fields only."""

    source_text: str
    entries: tuple[QidfEntry, ...]

    def serialize_preserved(self) -> str:
        return self.source_text

    @property
    def key_entries(self) -> tuple[QidfEntry, ...]:
        return tuple(entry for entry in self.entries if entry.kind == "key")

    def values(self, key: str) -> tuple[str, ...]:
        normalized = key.upper()
        return tuple(
            entry.raw_value or ""
            for entry in self.key_entries
            if entry.normalized_key == normalized
        )

    def value(self, key: str) -> str | None:
        values = self.values(key)
        return values[-1] if values else None

    @property
    def unknown_keys(self) -> tuple[str, ...]:
        return tuple(
            entry.original_key or ""
            for entry in self.key_entries
            if entry.normalized_key is not None and not _is_documented_key(entry.normalized_key)
        )

    @property
    def printer(self) -> str | None:
        return _nonempty(self.value("PRINTER"))

    @property
    def curve_name(self) -> str | None:
        return _nonempty(self.value("CURVE_NAME"))

    @property
    def default_ink_limit(self) -> Decimal | None:
        return _optional_real(self.value("DEFAULT_INK_LIMIT"), "DEFAULT_INK_LIMIT")

    @property
    def boost_k(self) -> Decimal | None:
        return _optional_real(self.value("BOOST_K"), "BOOST_K")

    @property
    def limits(self) -> dict[str, Decimal | None]:
        result: dict[str, Decimal | None] = {}
        for entry in self.key_entries:
            assert entry.normalized_key is not None
            match = _LIMIT_RE.fullmatch(entry.normalized_key)
            if match:
                result[match.group(1)] = _optional_real(entry.raw_value, entry.normalized_key)
        return result

    @property
    def copy_curves(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for entry in self.key_entries:
            assert entry.normalized_key is not None
            match = _COPY_RE.fullmatch(entry.normalized_key)
            if match and _nonempty(entry.raw_value) is not None:
                result[match.group(1)] = entry.raw_value.strip()
        return result

    @property
    def linearize_values(self) -> tuple[Decimal, ...] | None:
        value = self.value("LINEARIZE")
        if value is None or not value.strip():
            return None
        return _parse_real_sequence(value, "LINEARIZE")

    def partition(self, name: str) -> QidfPartition:
        prefix = name.upper()
        if prefix not in {"GRAY", "TONER", "TONER_2"}:
            raise ValueError(f"unknown QIDF partition {name!r}")
        inks: dict[int, str | None] = {}
        values: dict[int, Decimal | None] = {}
        for entry in self.key_entries:
            assert entry.normalized_key is not None
            ink_match = _PART_INK_RE.fullmatch(entry.normalized_key)
            value_match = _PART_VALUE_RE.fullmatch(entry.normalized_key)
            if ink_match and ink_match.group(1) == prefix:
                ordinal = int(ink_match.group(2))
                ink = _nonempty(entry.raw_value)
                inks[ordinal] = ink
            elif value_match and value_match.group(1) == prefix:
                ordinal = int(value_match.group(2))
                parsed = _optional_real(entry.raw_value, entry.normalized_key)
                values[ordinal] = parsed
        parts_list: list[QidfPartitionPart] = []
        for ordinal in sorted(set(inks) | set(values)):
            ink = inks.get(ordinal)
            relative_density = values.get(ordinal)
            if ink is None and relative_density is None:
                # Real QTR descriptors commonly retain blank paired slots up
                # to the printer's maximum channel count.
                continue
            if ink is None or relative_density is None:
                raise ValueError(
                    f"{prefix} partition ordinal {ordinal} has an unmatched INK/VAL pair"
                )
            parts_list.append(QidfPartitionPart(ordinal, ink, relative_density))
        parts = tuple(parts_list)
        count_key = f"N_OF_{prefix}_PARTS"
        count = _optional_integer(self.value(count_key), count_key)
        overlap = (
            _optional_real(self.value("GRAY_OVERLAP"), "GRAY_OVERLAP") if prefix == "GRAY" else None
        )
        return QidfPartition(
            name=prefix,
            parts=parts,
            declared_part_count=count,
            highlight=_optional_real(self.value(f"{prefix}_HIGHLIGHT"), f"{prefix}_HIGHLIGHT"),
            shadow=_optional_real(self.value(f"{prefix}_SHADOW"), f"{prefix}_SHADOW"),
            gamma=_optional_real(self.value(f"{prefix}_GAMMA"), f"{prefix}_GAMMA"),
            overlap=overlap,
            correction_raw=self.value(f"{prefix}_CURVE"),
        )


def _nonempty(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    return value.strip()


def _parse_real(value: str, key: str) -> Decimal:
    stripped = value.strip()
    if not _REAL_RE.fullmatch(stripped):
        raise ValueError(f"{key} must be an unsigned decimal value, got {value!r}")
    try:
        return Decimal(stripped.replace(",", "."))
    except InvalidOperation as error:  # defensive; regex already rejects this
        raise ValueError(f"{key} has malformed decimal value {value!r}") from error


def _optional_real(value: str | None, key: str) -> Decimal | None:
    if value is None or not value.strip():
        return None
    return _parse_real(value, key)


def _optional_integer(value: str | None, key: str) -> int | None:
    if value is None or not value.strip():
        return None
    stripped = value.strip()
    if not _INTEGER_RE.fullmatch(stripped):
        raise ValueError(f"{key} must be an unsigned integer, got {value!r}")
    return int(stripped)


def _parse_real_sequence(value: str, key: str) -> tuple[Decimal, ...]:
    stripped = value.strip()
    if len(stripped) < 2 or stripped[0] != '"' or stripped[-1] != '"':
        raise ValueError(f"{key} sequence must be enclosed in double quotes")
    items = stripped[1:-1].split()
    if not items:
        raise ValueError(f"{key} sequence must not be empty")
    return tuple(_parse_real(item, key) for item in items)


def _is_documented_key(key: str) -> bool:
    return bool(
        key
        in {
            "PRINTER",
            "CURVE_NAME",
            "N_OF_INKS",
            "N_OF_GRAY_PARTS",
            "N_OF_TONER_PARTS",
            "N_OF_TONER_2_PARTS",
            "N_OF_UNUSED",
            "GRAPH_CURVE",
            "CALIBRATION",
            "DEFAULT_INK_LIMIT",
            "BOOST_K",
            "LINEARIZE",
            "GRAY_CURVE",
            "GRAY_HIGHLIGHT",
            "GRAY_SHADOW",
            "GRAY_OVERLAP",
            "GRAY_GAMMA",
            "TONER_CURVE",
            "TONER_HIGHLIGHT",
            "TONER_SHADOW",
            "TONER_GAMMA",
            "TONER_2_CURVE",
            "TONER_2_HIGHLIGHT",
            "TONER_2_SHADOW",
            "TONER_2_GAMMA",
            "UC_NEUTRALIZER",
            "UC_NEUTRALIZER2",
        }
        or _LIMIT_RE.fullmatch(key)
        or _COPY_RE.fullmatch(key)
        or _PART_INK_RE.fullmatch(key)
        or _PART_VALUE_RE.fullmatch(key)
        or re.fullmatch(r"UNUSED_INK_[0-9]+", key)
        or re.fullmatch(r"CURVE_.+", key)
    )


def _validate_known_entry(entry: QidfEntry) -> None:
    assert entry.normalized_key is not None
    key = entry.normalized_key
    value = entry.raw_value or ""
    if key in _INTEGER_KEYS:
        _optional_integer(value, key)
    elif key in _REAL_KEYS or _LIMIT_RE.fullmatch(key) or _PART_VALUE_RE.fullmatch(key):
        _optional_real(value, key)
    elif key in _BOOL_KEYS and value.strip() and value.strip().upper() not in {"YES", "NO"}:
        raise ValueError(f"{key} must be YES or NO, got {value!r}")
    elif key in _SEQUENCE_KEYS and value.strip():
        _parse_real_sequence(value, key)
    ink_match = _PART_INK_RE.fullmatch(key)
    if ink_match and not 1 <= int(ink_match.group(2)) <= 10:
        raise ValueError(f"{key} partition ordinal must be in 1..10")


def parse_qidf(text: str) -> QidfDocument:
    """Parse QTR's INI-like descriptor while retaining every original line."""

    entries: list[QidfEntry] = []
    for line_number, raw in enumerate(text.splitlines(keepends=True), start=1):
        body = raw.rstrip("\r\n")
        semantic_body = body.lstrip("\ufeff") if line_number == 1 else body
        stripped = semantic_body.strip()
        if not stripped:
            entries.append(QidfEntry(line_number, raw, "blank"))
            continue
        if stripped.startswith("#"):
            entries.append(QidfEntry(line_number, raw, "comment"))
            continue
        if "=" not in semantic_body:
            # A small set of shipped QTR descriptors writes a documented key
            # alone to represent an empty value. Preserve that real INI variant
            # without accepting arbitrary malformed non-key lines.
            candidate = semantic_body.strip()
            if not _is_documented_key(candidate.upper()):
                raise ValueError(f"line {line_number}: expected KEY=value")
            original_key, raw_value = candidate, ""
        else:
            original_key, raw_value = semantic_body.split("=", 1)
        original_key = original_key.strip()
        if not original_key:
            raise ValueError(f"line {line_number}: empty QIDF key")
        entry = QidfEntry(
            line_number=line_number,
            raw=raw,
            kind="key",
            original_key=original_key,
            normalized_key=original_key.upper(),
            raw_value=raw_value,
        )
        if _is_documented_key(entry.normalized_key):
            _validate_known_entry(entry)
        entries.append(entry)
    return QidfDocument(text, tuple(entries))
