# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Deterministic Windows package-asset generator.

The generator consumes the authoritative PrinterRegistry and writes only the
generated capabilities, PDC/PDR files, and their catalog.  The checked-in UWP
Package.appxmanifest remains the single manifest source.
"""

from __future__ import annotations

import json
import re
from hashlib import sha256
from pathlib import Path

from xp750_rip.printers.capabilities import PrinterRegistry
from xp750_rip.printers.xp750_capabilities import default_registry
from xp750_rip.windows_psa import (
    export_capabilities,
    export_print_device_capabilities,
    export_print_device_resources,
)

MODULE_CATALOG_SCHEMA = "pytone-rip.windows-printer-catalog"
MODULE_DESCRIPTOR_SCHEMA = "pytone-rip.windows-printer-model"
MODULE_SCHEMA_VERSION = 2
CORE_CONTRACT_VERSION = 2
CORE_PACKAGE_IDENTITY = "pyToneRIP.Manager"
CORE_APP_SERVICE_NAME = "pytone-rip.core.v1"
SHARED_FOLDER_NAME = "pyToneRIP.Shared"

# Keep this in lockstep with Package.appxmanifest/Resources.  Underscores are
# used because PPD locale identifiers and PDR filenames follow that convention.
PDR_APP_LOCALES = (
    "bg_BG", "cs_CZ", "da_DK", "de_DE", "el_GR", "en_GB", "en_US",
    "es_ES", "et_EE", "fi_FI", "fr_FR", "ga_IE", "hr_HR", "hu_HU",
    "it_IT", "lt_LT", "lv_LV", "mt_MT", "nl_NL", "pl_PL", "pt_PT",
    "ro_RO", "ru_RU", "sk_SK", "sl_SI", "sv_SE", "uk_UA",
)


def _token(model_id: str) -> str:
    token = re.sub(r"[^A-Za-z0-9]", "", model_id)
    if not token or not token[0].isalpha():
        token = "Model" + token
    if len(token) > 48:
        raise ValueError(f"model ID is too long for a Windows package token: {model_id}")
    return token


def generate_windows_package_assets(
    output_dir: Path,
    *,
    version: str,
    registry: PrinterRegistry | None = None,
) -> dict[str, object]:
    """Generate deterministic package assets for every registry model."""

    if not re.fullmatch(r"\d+\.\d+\.\d+\.\d+", version):
        raise ValueError("Windows package version must contain four numeric components")
    selected = registry or default_registry()
    output_dir.mkdir(parents=True, exist_ok=True)
    if not selected.models:
        raise ValueError("At least one printer model is required for the workflow bootstrap PDC")
    package_assets = output_dir / "Assets"
    package_assets.mkdir(parents=True, exist_ok=True)
    modules: list[dict[str, object]] = []
    uris: set[str] = set()
    all_capabilities = export_capabilities(selected)
    (package_assets / "capabilities.generated.json").write_text(
        json.dumps(all_capabilities, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    # Preferences reads only its assigned model. Keep the complete export for
    # Manager, and split the same generated data for queue-scoped settings UI.
    (package_assets / "capabilities.common.json").write_text(
        json.dumps({**all_capabilities, "printers": []}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (package_assets / "capabilities.index.json").write_text(
        json.dumps(
            {
                **all_capabilities,
                "printers": [
                    {"id": printer["id"], "display_name": printer["display_name"]}
                    for printer in all_capabilities["printers"]
                ],
            },
            ensure_ascii=False,
        ) + "\n",
        encoding="utf-8",
    )
    capability_models = package_assets / "CapabilityModels"
    capability_models.mkdir(parents=True, exist_ok=True)
    for printer in all_capabilities["printers"]:
        (capability_models / f"model.{_token(printer['id'])}.json").write_text(
            json.dumps(printer, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    pdc_models = package_assets / "PdcModels"
    pdc_models.mkdir(parents=True, exist_ok=True)
    pdr_models = package_assets / "PdrModels"
    pdr_models.mkdir(parents=True, exist_ok=True)
    pdc_index: dict[str, str] = {}
    pdr_index: dict[str, dict[str, str]] = {}
    for model in selected.models:
        token = _token(model.identity)
        printer_uri = f"pytone-rip:{token.lower()}"
        if printer_uri.casefold() in uris:
            raise ValueError(f"Windows printer URI collision for {model.identity}")
        uris.add(printer_uri.casefold())

        pdc = export_print_device_capabilities(selected, model_id=model.identity)
        pdc_filename = f"model.{token}.pdc.xml"
        pdc_file = f"Assets\\PdcModels\\{pdc_filename}"
        pdc_path = pdc_models / pdc_filename
        pdc_path.write_text(pdc, encoding="utf-8")
        pdc_index[model.identity] = pdc_filename
        localized_pdrs: dict[str, str] = {}
        for locale in PDR_APP_LOCALES:
            pdr_filename = f"model.{token}.{locale}.pdr.xml"
            pdr_path = pdr_models / pdr_filename
            pdr_path.write_text(
                export_print_device_resources(
                    selected,
                    model_id=model.identity,
                    locale=locale,
                ),
                encoding="utf-8",
            )
            localized_pdrs[locale] = pdr_filename
        pdr_index[model.identity] = localized_pdrs
        descriptor = {
            "schema": MODULE_DESCRIPTOR_SCHEMA,
            "version": MODULE_SCHEMA_VERSION,
            "core_contract": CORE_CONTRACT_VERSION,
            "model_id": model.identity,
            "display_name": model.display_name,
            "printer_uri_root": printer_uri,
            "pdc_file": pdc_file,
            "pdr_files": {
                locale: f"Assets\\PdrModels\\{filename}"
                for locale, filename in localized_pdrs.items()
            },
            # Hash the bytes actually written on this platform. pathlib applies
            # native newline translation, and the catalog audits the exact bytes
            # later stored in the MSIX rather than an abstract Python string.
            "pdc_sha256": sha256(pdc_path.read_bytes()).hexdigest().upper(),
        }
        modules.append(descriptor)

    (pdc_models / "index.json").write_text(
        json.dumps(
            {
                "schema": "pytone-rip.model-pdc-index",
                "version": 1,
                "models": pdc_index,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (pdr_models / "index.json").write_text(
        json.dumps(
            {
                "schema": "pytone-rip.model-pdr-index",
                "version": 1,
                "models": pdr_index,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    catalog = {
        "schema": MODULE_CATALOG_SCHEMA,
        "version": MODULE_SCHEMA_VERSION,
        "core_contract": CORE_CONTRACT_VERSION,
        "package_version": version,
        "core": {
            "package_identity": CORE_PACKAGE_IDENTITY,
            "app_service": CORE_APP_SERVICE_NAME,
            "shared_folder": SHARED_FOLDER_NAME,
        },
        "modules": modules,
    }
    (output_dir / "catalog.json").write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return catalog
