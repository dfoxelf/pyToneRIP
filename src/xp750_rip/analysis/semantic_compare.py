# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Semantic comparison for the fixed Phase 3C model-96 candidate."""

from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256

from xp750_rip.analysis.escp2_parser import EvidenceClass, StreamRecord


class DifferenceClass(StrEnum):
    EXPECTED = "EXPECTED"
    ACCEPTABLE = "ACCEPTABLE"
    REQUIRES_FIX = "REQUIRES_FIX"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class ComparisonItem:
    subject: str
    golden: str
    candidate: str
    classification: DifferenceClass
    explanation: str


@dataclass(frozen=True, slots=True)
class SemanticComparison:
    items: tuple[ComparisonItem, ...]

    @property
    def has_required_fixes(self) -> bool:
        return any(item.classification is DifferenceClass.REQUIRES_FIX for item in self.items)


def _named(records: tuple[StreamRecord, ...], name: str) -> StreamRecord:
    matches = [record for record in records if record.name == name]
    if len(matches) != 1:
        raise ValueError(f"ожидалась ровно одна запись {name}, найдено {len(matches)}")
    return matches[0]


def _required_item(
    subject: str,
    golden: object,
    candidate: object,
    explanation: str,
) -> ComparisonItem:
    classification = (
        DifferenceClass.EXPECTED if golden == candidate else DifferenceClass.REQUIRES_FIX
    )
    return ComparisonItem(subject, repr(golden), repr(candidate), classification, explanation)


def compare_phase3c_to_golden(
    golden: tuple[StreamRecord, ...], candidate: tuple[StreamRecord, ...]
) -> SemanticComparison:
    """Compare protocol invariants, not binary identity or raster artwork bytes."""

    items: list[ComparisonItem] = []
    items.append(
        _required_item(
            "job framing",
            [record.name for record in golden],
            [record.name for record in candidate],
            "порядок всех структурных records должен совпадать",
        )
    )
    items.append(
        _required_item(
            "REMOTE1 structure",
            [record.name for record in golden if record.family == "REMOTE1"],
            [record.name for record in candidate if record.family == "REMOTE1"],
            "начальная и завершающая REMOTE1 sequences обязательны для профиля",
        )
    )
    items.append(
        _required_item(
            "unknown records",
            0,
            sum(record.confidence is EvidenceClass.UNKNOWN for record in candidate),
            "в candidate не допускается необъяснённая parser desynchronization",
        )
    )

    for name, keys, subject in (
        ("REMOTE_SN", ("payload_hex",), "REMOTE1 SN parameters"),
        ("REMOTE_PP", ("path_type", "path_number"), "REMOTE1 paper path"),
        (
            "REMOTE_MI",
            ("media_format", "media_type", "media_size"),
            "REMOTE1 media info",
        ),
        ("REMOTE_LD", ("payload_length",), "REMOTE1 load defaults"),
        ("REMOTE_JE", ("value",), "REMOTE1 job end"),
    ):
        golden_record = _named(golden, name)
        candidate_record = _named(candidate, name)
        items.append(
            _required_item(
                subject,
                tuple(golden_record.parameters[key] for key in keys),
                tuple(candidate_record.parameters[key] for key in keys),
                f"параметры {name} должны повторять проверенную REMOTE1 sequence",
            )
        )

    for name, keys, subject in (
        ("GRAPHICS_MODE", ("enabled",), "graphics mode"),
        ("UNIT_SETUP", ("page_unit", "vertical_unit", "horizontal_unit", "base_unit"), "units"),
        ("GRAY_MODE", ("payload_hex",), "gray mode"),
        ("MICROWEAVE", ("value",), "printer MicroWeave"),
        ("DIRECTION", ("value",), "printing direction"),
        ("DOT_SIZE", ("dot_size",), "dot size"),
        ("RESOLUTION", ("base_unit", "vertical_unit", "horizontal_unit"), "resolution"),
        ("PAGE_LENGTH", ("value",), "page length"),
        ("PAGE_FORMAT", ("top", "bottom"), "page format"),
        ("PAPER_DIMENSIONS", ("width", "height"), "paper dimensions"),
        ("PRINT_METHOD", ("value",), "print method"),
    ):
        golden_record = _named(golden, name)
        candidate_record = _named(candidate, name)
        golden_values = tuple(golden_record.parameters[key] for key in keys)
        candidate_values = tuple(candidate_record.parameters[key] for key in keys)
        items.append(
            _required_item(
                subject,
                golden_values,
                candidate_values,
                f"параметры {name} должны повторять проверенный model96 profile",
            )
        )

    golden_rasters = [record.raster for record in golden if record.raster is not None]
    candidate_rasters = [record.raster for record in candidate if record.raster is not None]
    items.append(
        _required_item(
            "pass count",
            len(golden_rasters),
            len(candidate_rasters),
            "ожидаются ровно два raster pass",
        )
    )
    for label, attribute in (
        ("channel selectors", "selector"),
        ("bit depth", "bit_depth"),
        ("compression", "compression"),
        ("rows per pass", "rows"),
        ("raster bytes per row", "bytes_per_row"),
    ):
        items.append(
            _required_item(
                label,
                [getattr(raster, attribute) for raster in golden_rasters],
                [getattr(raster, attribute) for raster in candidate_rasters],
                "raster header invariant physically verified by the golden job",
            )
        )

    golden_feeds = [
        record.parameters["value"] for record in golden if record.name == "RELATIVE_VERTICAL"
    ]
    candidate_feeds = [
        record.parameters["value"] for record in candidate if record.name == "RELATIVE_VERTICAL"
    ]
    items.append(
        _required_item(
            "position/feed commands",
            golden_feeds,
            candidate_feeds,
            "каждому pass предшествует ESC (v с advance 180",
        )
    )
    items.append(
        _required_item(
            "page and job termination",
            [record.name for record in golden[-6:]],
            [record.name for record in candidate[-6:]],
            "FF, reset и финальная REMOTE1 sequence должны совпадать",
        )
    )

    golden_label = _named(golden, "REMOTE_JS").parameters.get("label")
    candidate_label = _named(candidate, "REMOTE_JS").parameters.get("label")
    items.append(
        ComparisonItem(
            "REMOTE1 JS label",
            repr(golden_label),
            repr(candidate_label),
            (
                DifferenceClass.EXPECTED
                if golden_label == candidate_label
                else DifferenceClass.ACCEPTABLE
            ),
            "job label не относится к raster mechanics; candidate явно обозначает наш encoder",
        )
    )

    golden_payload_hashes = [
        sha256(raster.compressed_payload).hexdigest() for raster in golden_rasters
    ]
    candidate_payload_hashes = [
        sha256(raster.compressed_payload).hexdigest() for raster in candidate_rasters
    ]
    items.append(
        ComparisonItem(
            "raster payload",
            repr(golden_payload_hashes),
            repr(candidate_payload_hashes),
            DifferenceClass.EXPECTED,
            "candidate pattern построен математически; binary raster identity не требуется",
        )
    )
    golden_compressed_lengths = [len(raster.compressed_payload) for raster in golden_rasters]
    candidate_compressed_lengths = [len(raster.compressed_payload) for raster in candidate_rasters]
    items.append(
        ComparisonItem(
            "compressed raster lengths",
            repr(golden_compressed_lengths),
            repr(candidate_compressed_lengths),
            (
                DifferenceClass.EXPECTED
                if golden_compressed_lengths == candidate_compressed_lengths
                else DifferenceClass.ACCEPTABLE
            ),
            "PackBits length зависит от самостоятельно созданного pattern",
        )
    )
    golden_length = golden[-1].end_offset if golden else 0
    candidate_length = candidate[-1].end_offset if candidate else 0
    items.append(
        ComparisonItem(
            "PRN byte length",
            repr(golden_length),
            repr(candidate_length),
            DifferenceClass.EXPECTED
            if golden_length == candidate_length
            else DifferenceClass.ACCEPTABLE,
            "длина отличается из-за JS label и собственного compressed raster",
        )
    )
    return SemanticComparison(tuple(items))


def format_semantic_comparison(comparison: SemanticComparison) -> str:
    lines = [
        "СЕМАНТИЧЕСКОЕ СРАВНЕНИЕ PHASE 3C С GOLDEN REFERENCE",
        "",
        "КЛАСС        ПАРАМЕТР",
    ]
    for item in comparison.items:
        lines.extend(
            (
                f"{item.classification:<12} {item.subject}",
                f"  golden:    {item.golden}",
                f"  candidate: {item.candidate}",
                f"  пояснение: {item.explanation}",
            )
        )
    lines.extend(
        (
            "",
            "Критические REQUIRES_FIX: "
            + ("ЕСТЬ" if comparison.has_required_fixes else "ОТСУТСТВУЮТ"),
        )
    )
    return "\n".join(lines) + "\n"
