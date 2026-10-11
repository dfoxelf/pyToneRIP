# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Small preservation-oriented PPD capability reader.

This is deliberately an option/capability parser, not a PostScript interpreter and
not a printer backend.  It extracts the parts needed by the RIP while retaining the
source text for diagnostics.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_DIRECTIVE_RE = re.compile(
    r"^\*(?P<keyword>[^\s:]+)(?:\s+(?P<choice>[^/:\s]+)(?:/(?P<label>.*?))?)?:\s*(?P<value>.*)$"
)
_OPEN_UI_RE = re.compile(r"^\*OpenUI\s+\*(?P<name>[^/:]+)(?:/(?P<label>.*?))?:\s*(?P<kind>\S+)\s*$")
_CONSTRAINT_ITEM_RE = re.compile(r"\*([^\s]+)\s+([^\s]+)")
_PAIR_RE = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)")
_PPD_HEX_ESCAPE_RE = re.compile(r"<([0-9A-Fa-f]{2})>")
_PAGE_SIZE_RE = re.compile(
    r"/PageSize\s*\[\s*(?P<width>[-+]?\d+(?:\.\d+)?)\s+"
    r"(?P<height>[-+]?\d+(?:\.\d+)?)\s*\]"
)


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1]
    return value


def _decode_label(value: str) -> str:
    """Decode PPD's byte escapes used in human-facing option labels."""

    decoded = _PPD_HEX_ESCAPE_RE.sub(
        lambda match: bytes((int(match.group(1), 16),)).decode("latin-1"), value
    )
    # ReferenceDriver PPDs wrap many display labels in escaped quotes (\"A4\").
    # These delimiters are PPD syntax, not characters for the PDR or GUI.
    return _unquote(decoded.replace('\\"', '"'))


@dataclass(frozen=True, slots=True)
class PpdChoice:
    name: str
    label: str
    code: str


@dataclass(frozen=True, slots=True)
class PpdOption:
    name: str
    label: str
    kind: str
    default: str | None
    choices: tuple[PpdChoice, ...]

    def choice(self, name: str) -> PpdChoice:
        for item in self.choices:
            if item.name == name:
                return item
        raise KeyError(name)


@dataclass(frozen=True, slots=True)
class PpdConstraint:
    first_option: str
    first_choice: str
    second_option: str
    second_choice: str

    def rejects(self, selections: dict[str, str]) -> bool:
        return (
            selections.get(self.first_option) == self.first_choice
            and selections.get(self.second_option) == self.second_choice
        )


@dataclass(frozen=True, slots=True)
class PpdLocalizedLabel:
    """A translated PPD option or option-choice label."""

    locale: str
    option: str
    choice: str | None
    label: str


@dataclass(frozen=True, slots=True)
class PpdPage:
    name: str
    label: str
    width_points: float
    height_points: float
    imageable_left_points: float
    imageable_bottom_points: float
    imageable_right_points: float
    imageable_top_points: float

    @property
    def borderless(self) -> bool:
        tolerance = 0.05
        return (
            abs(self.imageable_left_points) <= tolerance
            and abs(self.imageable_bottom_points) <= tolerance
            and abs(self.imageable_right_points - self.width_points) <= tolerance
            and abs(self.imageable_top_points - self.height_points) <= tolerance
        )

    @property
    def width_mm(self) -> float:
        return self.width_points * 25.4 / 72.0

    @property
    def height_mm(self) -> float:
        return self.height_points * 25.4 / 72.0

    @property
    def printable_width_points(self) -> float:
        return self.imageable_right_points - self.imageable_left_points

    @property
    def printable_height_points(self) -> float:
        return self.imageable_top_points - self.imageable_bottom_points


@dataclass(frozen=True, slots=True)
class PpdCapabilities:
    source_text: str
    metadata: dict[str, str]
    options: tuple[PpdOption, ...]
    constraints: tuple[PpdConstraint, ...]
    pages: tuple[PpdPage, ...]
    localized_labels: tuple[PpdLocalizedLabel, ...]

    def option(self, name: str) -> PpdOption:
        for item in self.options:
            if item.name == name:
                return item
        raise KeyError(name)

    def page(self, name: str) -> PpdPage:
        for item in self.pages:
            if item.name == name:
                return item
        raise KeyError(name)

    def localized_label(
        self, locale: str, option: str, choice: str | None = None
    ) -> str | None:
        """Return the PPD-native localized label, if the PPD supplies one."""

        normalized = locale.replace("-", "_").casefold()
        candidates = (normalized, normalized.split("_", 1)[0])
        for candidate in candidates:
            for item in self.localized_labels:
                if (
                    item.locale.casefold() == candidate
                    and item.option == option
                    and item.choice == choice
                ):
                    return item.label
        return None

    @property
    def locales(self) -> tuple[str, ...]:
        return tuple(sorted({item.locale for item in self.localized_labels}))

    def validate_choices(self, selections: dict[str, str]) -> tuple[PpdConstraint, ...]:
        unknown_options = set(selections) - {option.name for option in self.options}
        if unknown_options:
            raise ValueError(
                "Неизвестные PPD options: " + ", ".join(sorted(unknown_options))
            )
        for option_name, choice_name in selections.items():
            self.option(option_name).choice(choice_name)
        return tuple(item for item in self.constraints if item.rejects(selections))


@dataclass(slots=True)
class _MutableOption:
    label: str
    kind: str
    choices: list[PpdChoice]


def _numbers(value: str, expected: int, directive: str) -> tuple[float, ...]:
    parsed = tuple(float(item) for item in _PAIR_RE.findall(_unquote(value)))
    if len(parsed) != expected:
        raise ValueError(f"PPD directive {directive} должен содержать {expected} чисел")
    return parsed


def parse_ppd(text: str) -> PpdCapabilities:
    """Parse generic PPD choices, defaults, constraints and page geometry."""

    metadata: dict[str, str] = {}
    defaults: dict[str, str] = {}
    mutable_options: dict[str, _MutableOption] = {}
    current_option: str | None = None
    constraints: list[PpdConstraint] = []
    dimensions: dict[str, tuple[float, float]] = {}
    imageable: dict[str, tuple[float, float, float, float]] = {}
    page_labels: dict[str, str] = {}
    localized_labels: list[PpdLocalizedLabel] = []

    for line_number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("*%"):
            continue
        open_match = _OPEN_UI_RE.match(line)
        if open_match:
            name = open_match.group("name")
            if name in mutable_options:
                raise ValueError(f"PPD line {line_number}: повторный OpenUI {name}")
            mutable_options[name] = _MutableOption(
                label=(open_match.group("label") or name).strip(),
                kind=open_match.group("kind"),
                choices=[],
            )
            current_option = name
            continue
        if line.startswith("*CloseUI:"):
            current_option = None
            continue
        if line.startswith("*UIConstraints:"):
            items = _CONSTRAINT_ITEM_RE.findall(line.split(":", 1)[1])
            if len(items) != 2:
                raise ValueError(f"PPD line {line_number}: неподдерживаемый UIConstraints")
            constraints.append(PpdConstraint(items[0][0], items[0][1], items[1][0], items[1][1]))
            continue

        match = _DIRECTIVE_RE.match(line)
        if not match:
            continue
        keyword = match.group("keyword")
        choice = match.group("choice")
        label = _decode_label((match.group("label") or choice or "").strip())
        value = _unquote(match.group("value"))

        if "." in keyword:
            locale, localized_keyword = keyword.split(".", 1)
            if localized_keyword == "Translation" and choice:
                localized_labels.append(
                    PpdLocalizedLabel(locale, choice, None, label)
                )
            elif choice:
                localized_labels.append(
                    PpdLocalizedLabel(locale, localized_keyword, choice, label)
                )
            continue
        if keyword.startswith("Default") and choice is None:
            defaults[keyword.removeprefix("Default")] = value
            continue
        if keyword == "PaperDimension" and choice:
            width, height = _numbers(value, 2, keyword)
            dimensions[choice] = (width, height)
            continue
        if keyword == "ImageableArea" and choice:
            left, bottom, right, top = _numbers(value, 4, keyword)
            imageable[choice] = (left, bottom, right, top)
            continue
        if keyword == "PageSize" and choice:
            page_labels[choice] = label
            # Some generated PPDs contain a valid PageSize setpagedevice value
            # but omit PaperDimension/ImageableArea. Preserve that geometry so
            # the pyToneRIP PDC/PDR generator can still produce real pages.
            embedded_size = _PAGE_SIZE_RE.search(value)
            if embedded_size and choice not in dimensions:
                dimensions[choice] = (
                    float(embedded_size.group("width")),
                    float(embedded_size.group("height")),
                )

        if current_option == keyword and choice:
            mutable_options[current_option].choices.append(PpdChoice(choice, label, value))
        elif choice is None and keyword not in {
            "OrderDependency",
            "HWMargins",
            "Font",
        }:
            metadata[keyword] = value

    options = tuple(
        PpdOption(name, option.label, option.kind, defaults.get(name), tuple(option.choices))
        for name, option in mutable_options.items()
    )
    pages: list[PpdPage] = []
    for name, (width, height) in dimensions.items():
        # Missing ImageableArea is unknown, not evidence of edge-to-edge
        # printing. Generated PPDs often omit it for every ordinary sheet.
        fallback_margin = (
            0.0 if "borderless" in page_labels.get(name, "").casefold()
            else min(9.0, width / 20, height / 20)
        )
        left, bottom, right, top = imageable.get(
            name,
            (fallback_margin, fallback_margin,
             width - fallback_margin, height - fallback_margin),
        )
        pages.append(
            PpdPage(
                name=name,
                label=page_labels.get(name, name),
                width_points=width,
                height_points=height,
                imageable_left_points=left,
                imageable_bottom_points=bottom,
                imageable_right_points=right,
                imageable_top_points=top,
            )
        )
    return PpdCapabilities(
        text, metadata, options, tuple(constraints), tuple(pages),
        tuple(localized_labels),
    )


def read_ppd(path: Path) -> PpdCapabilities:
    """Read a PPD while retaining its genuine localized labels.

    Some Epson PPDs mix valid UTF-8 translations with isolated legacy bytes.
    Preserve the valid UTF-8 spans instead of decoding the entire file as
    Latin-1 because of one invalid byte; retain the latter as Latin-1 chars.
    """

    content = path.read_bytes()
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        decoded = content.decode("utf-8", errors="surrogateescape")
        text = "".join(
            chr(ord(character) - 0xDC00)
            if 0xDC80 <= ord(character) <= 0xDCFF else character
            for character in decoded
        )
    return parse_ppd(text)
