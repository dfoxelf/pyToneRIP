# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Read-only inventory and compatibility audit of the supplied QTR tree."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from PIL import Image

from xp750_rip.qtr.qidf import parse_qidf
from xp750_rip.qtr.quad import parse_quad


def _relative_paths(paths: list[Path], root: Path) -> list[str]:
    return [path.relative_to(root).as_posix() for path in sorted(paths)]


def inventory_qtr_tree(root: Path) -> dict[str, object]:
    files = [path for path in root.rglob("*") if path.is_file()]
    by_suffix = Counter(path.suffix.lower() or "<none>" for path in files)
    executables = [path for path in files if path.suffix.lower() == ".exe"]
    quads = [path for path in files if path.suffix.lower() == ".quad"]
    qidfs = [path for path in files if path.suffix.lower() == ".qidf"]
    txts = [path for path in files if path.suffix.lower() == ".txt"]
    ppds = [path for path in files if path.suffix.lower() == ".ppd"]
    tiffs = [path for path in files if path.suffix.lower() in {".tif", ".tiff"}]
    documentation = [
        path for path in files if path.suffix.lower() in {".pdf", ".chm", ".rtf"}
    ]
    configuration = [
        path
        for path in files
        if path.suffix.lower() in {".txtqidf", ".ppd", ".qidf"}
        or "config" in path.name.lower()
    ]
    calibration = [
        path
        for path in files
        if path.suffix.lower() in {".raw", ".amp", ".icc", ".psd"}
        or any(
            marker in path.as_posix().lower()
            for marker in ("inksep", "inkseparation", "linear", "step", "bullseye")
        )
    ]
    model_resources = [
        path
        for path in files
        if path.is_relative_to(root / "Profiles") or path.is_relative_to(root / "QuadTone")
    ]
    classified = set(
        executables
        + quads
        + qidfs
        + txts
        + ppds
        + tiffs
        + documentation
        + configuration
        + calibration
        + model_resources
    )
    unknown = [path for path in files if path not in classified]
    profile_models = sorted(path.name for path in (root / "Profiles").iterdir() if path.is_dir())
    runtime_models = sorted(path.name for path in (root / "QuadTone").iterdir() if path.is_dir())
    groups = {
        "executables": _relative_paths(executables, root),
        "quad": _relative_paths(quads, root),
        "qidf": _relative_paths(qidfs, root),
        "txt": _relative_paths(txts, root),
        "ppd": _relative_paths(ppds, root),
        "tiff": _relative_paths(tiffs, root),
        "printer_model_resources": _relative_paths(model_resources, root),
        "documentation": _relative_paths(documentation, root),
        "configuration": _relative_paths(configuration, root),
        "calibration_inkseparation": _relative_paths(calibration, root),
        "unknown_resources": _relative_paths(unknown, root),
    }
    return {
        "root": root.as_posix(),
        "file_count": len(files),
        "extension_counts": dict(sorted(by_suffix.items())),
        "groups_are_semantic_and_may_overlap": True,
        "groups": groups,
        "group_counts": {name: len(items) for name, items in groups.items()},
        "profile_model_directories": profile_models,
        "runtime_model_directories": runtime_models,
    }


def _read_descriptor_text(path: Path) -> tuple[str, str]:
    data = path.read_bytes()
    try:
        return data.decode("utf-8-sig"), "utf-8-sig"
    except UnicodeDecodeError:
        return data.decode("cp1252"), "cp1252"


def audit_qtr_corpus(root: Path) -> dict[str, object]:
    quad_files = sorted(root.rglob("*.quad"))
    curve_counts: Counter[int] = Counter()
    ink_orders: Counter[tuple[str, ...]] = Counter()
    compact_count = 0
    quad_errors: list[dict[str, str]] = []
    for path in quad_files:
        try:
            profile = parse_quad(path.read_text(encoding="utf-8-sig"))
            curve_counts[len(profile.curves)] += 1
            ink_orders[profile.ink_codes] += 1
            header = next(line.raw for line in profile.lines if line.kind == "header")
            compact_count += int("," not in header)
        except (OSError, UnicodeError, ValueError) as error:
            quad_errors.append({"path": path.relative_to(root).as_posix(), "error": str(error)})

    qidf_files = sorted([*root.rglob("*.qidf"), *root.rglob("*.txtqidf")])
    qidf_errors: list[dict[str, str]] = []
    encodings: Counter[str] = Counter()
    partition_counts: Counter[tuple[int, int, int]] = Counter()
    unknown_keys: Counter[str] = Counter()
    for path in qidf_files:
        try:
            text, encoding = _read_descriptor_text(path)
            encodings[encoding] += 1
            descriptor = parse_qidf(text)
            partition_counts[
                tuple(
                    len(descriptor.partition(name).parts)
                    for name in ("GRAY", "TONER", "TONER_2")
                )
            ] += 1
            unknown_keys.update(descriptor.unknown_keys)
        except (OSError, UnicodeError, ValueError) as error:
            qidf_errors.append({"path": path.relative_to(root).as_posix(), "error": str(error)})
    return {
        "quad": {
            "file_count": len(quad_files),
            "parsed_count": len(quad_files) - len(quad_errors),
            "error_count": len(quad_errors),
            "errors": quad_errors,
            "compiled_samples_per_curve": 256,
            "numeric_domain": [0, 65535],
            "compact_header_profile_count": compact_count,
            "curve_count_distribution": {
                str(key): value for key, value in sorted(curve_counts.items())
            },
            "ink_order_distribution": [
                {"ink_order": list(order), "count": count}
                for order, count in ink_orders.most_common()
            ],
        },
        "qidf": {
            "file_count_including_txtqidf": len(qidf_files),
            "parsed_count": len(qidf_files) - len(qidf_errors),
            "error_count": len(qidf_errors),
            "errors": qidf_errors,
            "decoded_encodings": dict(encodings),
            "partition_count_distribution": [
                {"gray_toner_toner2": list(signature), "count": count}
                for signature, count in partition_counts.most_common()
            ],
            "preserved_unknown_keys": dict(unknown_keys),
        },
    }


def inspect_qtr_tiffs(root: Path) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for path in sorted([*root.rglob("*.tif"), *root.rglob("*.tiff")]):
        with Image.open(path) as image:
            bits = image.tag_v2.get(258)
            dpi_value = image.info.get("dpi")
            try:
                extrema: object = image.getextrema()
            except ValueError:
                extrema = None
            result.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "dimensions": list(image.size),
                    "pillow_mode": image.mode,
                    "bits_per_sample": list(bits) if isinstance(bits, tuple) else bits,
                    "photometric_interpretation": image.tag_v2.get(262),
                    "orientation": image.tag_v2.get(274, 1),
                    "compression": image.tag_v2.get(259, 1),
                    "dpi": [float(value) for value in dpi_value]
                    if isinstance(dpi_value, tuple)
                    else None,
                    "has_icc_profile": bool(image.info.get("icc_profile")),
                    "sample_extrema": list(extrema)
                    if isinstance(extrema, tuple) and all(isinstance(item, int) for item in extrema)
                    else extrema,
                    "purpose": _infer_tiff_purpose(path),
                }
            )
    return result


def _infer_tiff_purpose(path: Path) -> dict[str, str]:
    name = path.as_posix().lower()
    if "inksep" in name or "inkseparation" in name:
        purpose = "InkSeparation target"
    elif "21step" in name or "step-21" in name:
        purpose = "21-step grayscale target"
    elif "51" in name and "step" in name:
        purpose = "51-step grayscale target"
    elif "100step" in name:
        purpose = "100-step grayscale target"
    elif "bullseye" in name:
        purpose = "Eye-One bullseye target"
    elif "gamma" in name:
        purpose = "gamma conversion resource"
    else:
        purpose = "UNKNOWN"
    return {"value": purpose, "classification": "INFERENCE"}
