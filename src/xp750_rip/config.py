# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""TOML user configuration and profile-preset loading."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

type TomlScalar = str | int | float | bool
type TomlValue = TomlScalar | list[object] | dict[str, object]

_SECTIONS = {
    "printer",
    "transport",
    "print",
    "profile",
    "qtr",
    "halftone",
    "routing",
    "inkset",
    "defaults",
}
_SCALAR_SECTIONS = _SECTIONS - {"qtr"}


@dataclass(frozen=True, slots=True)
class UserConfig:
    source_path: Path
    sections: dict[str, dict[str, TomlValue]]

    def section(self, name: str) -> dict[str, TomlValue]:
        return dict(self.sections.get(name, {}))

    @property
    def qtr(self) -> dict[str, TomlValue]:
        return self.section("qtr")

    @property
    def routing(self) -> dict[str, str]:
        values = self.section("routing")
        if any(not isinstance(value, str) for value in values.values()):
            raise ValueError("Все значения [routing] должны быть строковыми PhysicalPath IDs")
        return {key: str(value) for key, value in values.items()}

    @property
    def inkset(self) -> dict[str, str]:
        values = self.section("inkset")
        if any(not isinstance(value, str) for value in values.values()):
            raise ValueError("Все значения [inkset] должны быть строками")
        return {key: str(value) for key, value in values.items()}

    def path_value(self, section: str, key: str) -> Path | None:
        value = self.section(section).get(key)
        if value is None:
            return None
        if not isinstance(value, str):
            raise TypeError(f"[{section}] {key} должен быть путём-строкой")
        path = Path(value)
        return path if path.is_absolute() else self.source_path.parent / path

    def resolve_path(self, value: object, *, context: str) -> Path:
        if not isinstance(value, str):
            raise TypeError(f"{context} должен быть путём-строкой")
        path = Path(value)
        return path if path.is_absolute() else self.source_path.parent / path


def load_user_config(path: Path) -> UserConfig:
    with path.open("rb") as stream:
        parsed = tomllib.load(stream)
    unknown = set(parsed) - _SECTIONS
    if unknown:
        raise ValueError("Неизвестные TOML sections: " + ", ".join(sorted(unknown)))
    sections: dict[str, dict[str, TomlValue]] = {}
    for name, value in parsed.items():
        if not isinstance(value, dict):
            raise TypeError(f"TOML [{name}] должен быть таблицей")
        if name in _SCALAR_SECTIONS and any(
            isinstance(item, (dict, list)) for item in value.values()
        ):
            raise ValueError(f"TOML [{name}] поддерживает только scalar values")
        sections[name] = dict(value)
    return UserConfig(path.resolve(), sections)
