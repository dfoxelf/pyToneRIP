# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Load the self-contained, versioned WorkForce native geometry records."""

from __future__ import annotations

from dataclasses import fields, replace
from fractions import Fraction
from typing import Any

from xp750_rip.printers.capabilities import (
    EpsonEscp2BackendProfile,
    NativeHeadBank,
    NativeRasterSetup,
    PageCapability,
    PrinterMode,
    PrinterModel,
)

SCHEMA = "pytone-rip.native-workforce-v1"
PHOTO_MODE_ID = "Photo_1200x1200_paper_X"
PHOTO_FINE_MODE_ID = "pyToneRIP_PhotoFine_2400x1200"
_PHOTO_FINE_MODELS = frozenset({
    "WorkForceWF3620", "WorkForceWF3640", "WorkForceWF7110",
    "WorkForceWF7610", "WorkForceWF7620",
})


def _experimental_raster_setup(base, horizontal_dpi, vertical_dpi):
    """Rescale movement and row coordinates independently of sheet units.

    ESC/P-R input DPI does not specify an ESC/P2 head grid. Retain this
    model's measured nozzle pitch and registration rather than substituting
    the OEM driver's RGB input resolution or declared maximum output DPI.
    """
    original = base.native_raster_setup
    assert original is not None
    old_scale = int.from_bytes(original.units_payload[3:], "little")
    old_page_units = old_scale // original.units_payload[0]
    old_vertical_units = old_scale // original.units_payload[1]
    old_head_scale = int.from_bytes(original.resolution_payload[:2], "little")
    head_scale = 14400

    def integral(value):
        result = Fraction(value)
        if result.denominator != 1:
            raise ValueError("Experimental WorkForce grid requires integral coordinates")
        return result.numerator

    movement_ratio = Fraction(vertical_dpi, old_vertical_units)
    row_ratio = Fraction(vertical_dpi, base.vertical_dpi)
    units = bytes(integral(Fraction(head_scale, dpi))
                  for dpi in (old_page_units, vertical_dpi, horizontal_dpi))
    pitch = bytes(integral(Fraction(head_scale * value, old_head_scale))
                  for value in original.resolution_payload[2:])
    return replace(
        original,
        units_payload=units + head_scale.to_bytes(2, "little"),
        resolution_payload=head_scale.to_bytes(2, "little") + pitch,
        origin_vertical_units=integral(original.origin_vertical_units * movement_ratio),
        channel_offsets=tuple((key, integral(value * movement_ratio))
                              for key, value in original.channel_offsets),
        black_only_channel_offset=(
            integral(original.black_only_channel_offset * movement_ratio)
            if original.black_only_channel_offset is not None else None),
        banks=tuple(replace(
            bank,
            offset_command_units=integral(bank.offset_command_units * movement_ratio),
            row_group=integral(bank.row_group * row_ratio),
        ) for bank in original.banks),
    )


def _workforce_photo_modes(package, modes):
    """Extend each compatible model's own registered Photo mode."""
    if package.model_id not in _PHOTO_FINE_MODELS:
        return modes
    base = next(mode for mode in modes if mode.identity == PHOTO_MODE_ID and mode.implemented)
    fine = replace(
        base,
        identity=PHOTO_FINE_MODE_ID,
        display_name="Super Photo Fine 2400 x 1200 dpi PyToneRIP",
        horizontal_dpi=2400,
        physical_horizontal_dpi=2400,
        physical_vertical_dpi=1200,
        horizontal_passes=8,
        vertical_passes=1,
        ordered_matrix_id="matrix-1x1",
        native_raster_setup=_experimental_raster_setup(base, 2400, 1200),
        output_resolution_dpi=(2400, 1200),
        offline_validated=False,
        escp2_default_direction="unidirectional",
        source="pyToneRIP host weave derived from the registered Photo mode",
    )
    return (*modes, fine)


def _matte_media_labels(source, media):
    """Name a shared coated-paper code after its explicit matte descriptor.

    Keep the canonical IDs, mode membership and aliases: saved Inkjet Paper
    selections still address exactly the same native media code.
    """
    matte_names = {}
    for item in source.get("media", ()):
        if "matte" in item["name"].casefold():
            code = item["media_mechanics"]["remote_media_type_code"]
            matte_names.setdefault(int(code), item["name"])
    return tuple(
        replace(item, display_name=matte_names[item.media_code], media_class="matte")
        if item.media_code in matte_names and "inkjet" in item.display_name.casefold()
        else item for item in media
    )


def _media_resolution_policy(package, data, modes, media):
    policy = data.get("media_resolution_policy", "descriptor")
    if policy == "descriptor":
        if not any(mode.identity == PHOTO_FINE_MODE_ID for mode in modes):
            return media
        return tuple(replace(
            medium,
            supported_modes=(*medium.supported_modes, PHOTO_FINE_MODE_ID),
            print_methods=(*medium.print_methods, *(
                (PHOTO_FINE_MODE_ID, method) for mode_id, method in medium.print_methods
                if mode_id == PHOTO_MODE_ID
            )),
        ) if PHOTO_MODE_ID in medium.supported_modes else medium for medium in media)
    if policy != "all_existing_resolutions" or package.model_id != "WorkForceWF3620":
        raise ValueError("Cross-media resolution selection is enabled only for WF-3620")
    available = {mode.identity: mode for mode in modes if mode.implemented}
    mechanics = tuple(field.name for field in fields(PrinterMode)
                      if field.name not in {"identity", "display_name", "source", "media_class"})
    signatures = {key: tuple(getattr(mode, name) for name in mechanics)
                  for key, mode in available.items()}
    result = []
    for medium in media:
        supported = list(medium.supported_modes)
        offered = {signatures[key] for key in supported if key in available}
        methods = dict(medium.print_methods)
        for mode in available.values():
            signature = signatures[mode.identity]
            if signature in offered:
                continue
            supported.append(mode.identity)
            offered.add(signature)
            if mode.escp2_print_method_override is not None:
                methods[mode.identity] = mode.escp2_print_method_override
        result.append(replace(medium, supported_modes=tuple(supported),
                              print_methods=tuple(methods.items())))
    return tuple(result)


def _workforce_page_geometry(package, pages, ppd):
    """Correct ISO A5 and the related models' #10, DL and C6 rear margins."""
    result = []
    for page in pages:
        sheet = page.ppd
        if sheet.name == "A5" and not sheet.borderless:
            width, height = 148 * 72 / 25.4, 210 * 72 / 25.4
            # Keep existing margins and the stable paper/firmware selector.
            sheet = replace(
                sheet, width_points=width, height_points=height,
                imageable_right_points=width - (
                    sheet.width_points - sheet.imageable_right_points),
                imageable_top_points=height - (
                    sheet.height_points - sheet.imageable_top_points),
            )
            page = replace(page, ppd=sheet, physical_media_size_mm=(148.0, 210.0),
                           native_page_format_mils=(
                               round(148 / 0.0254), round(210 / 0.0254),
                               page.native_page_format_mils[2]))
        elif (package.model_id in _PHOTO_FINE_MODELS
              and sheet.name in {"Envelope_10", "Envelope_DL", "Envelope_C6"}):
            # Each of these three formats was captured independently. Do not
            # infer the margin of an untested custom envelope from its name.
            sheet = replace(sheet, imageable_bottom_points=20 * 72 / 25.4)
            width, height, _ = page.native_page_format_mils
            page = replace(page, ppd=sheet,
                           native_page_format_mils=(width, height, round(20 / 0.0254)))
        result.append(page)
    by_name = {page.ppd.name: page.ppd for page in result}
    return result, replace(ppd, pages=tuple(by_name.get(page.name, page) for page in ppd.pages))


def load_native_workforce(package, source, runtime, ppd) -> PrinterModel:
    # Reuse the normal typed media/settings decoders. The model-specific page
    # geometry below deliberately bypasses synthetic-PPD repair heuristics.
    from xp750_rip.printers.catalog_models import (
        _feeds,
        _identifier,
        _media,
        _physical_paths,
        _settings,
        _vendor_modes,
    )

    data: dict[str, Any] = runtime["native_model"]
    if data.get("schema") != SCHEMA:
        raise ValueError("Unsupported native WorkForce geometry schema")
    if not package.package_id.startswith("pf--Epson_WorkForce"):
        raise ValueError("Native WorkForce geometry is restricted to WF packages")
    records = runtime["modes"]
    # _vendor_modes decodes the common scoped mode schema. Every inferred
    # head/grid field is replaced below with this mode's explicit native data.
    modes = _vendor_modes(source, runtime, 1, protocol_family="WF7610")
    by_scope = {}
    for record in records:
        app = record["app_raster_dpi"]
        scope = record["applicability"]
        media = "X" if scope["media_scope"] == "all_non_default_media" else str(scope["media_index"])
        key = (record["display_name"], app["horizontal"], app["vertical"], media)
        if key in by_scope:
            raise ValueError(f"Ambiguous WorkForce mode {key}")
        by_scope[key] = record
    result = []
    for mode in modes:
        matching = [r for (name, x, y, media), r in by_scope.items()
                    if mode.identity == _identifier(f"{name}_{x}x{y}_paper_{media}", "Mode")]
        if len(matching) != 1:
            raise ValueError(f"Missing unique native WorkForce mode: {mode.identity}")
        raw = matching[0]["native_geometry"]
        raster = matching[0]["dither_raster_dpi"]
        mode = replace(mode, horizontal_dpi=int(raster["horizontal"]),
                       vertical_dpi=int(raster["vertical"]))
        if package.model_id == "WorkForceWF3620":
            output = mode.output_resolution_dpi or (mode.physical_horizontal_dpi, mode.physical_vertical_dpi)
            label = f"{matching[0]['display_name']} — {output[0]} x {output[1]} DPI"
            if output != (mode.horizontal_dpi, mode.vertical_dpi):
                label += f" (raster {mode.horizontal_dpi} x {mode.vertical_dpi} DPI)"
            mode = replace(mode, display_name=label)
        setup = NativeRasterSetup(
            bytes.fromhex(raw["units_payload_hex"]),
            bytes.fromhex(raw["resolution_payload_hex"]),
            int(raw.get("full_head_origin_vertical_units", raw["sheet_origin_vertical_units"])),
            int(raw["reference_top_margin_mils"]),
            tuple((int(p["selector"]), int(p["offset_command_units"]))
                  for p in raw.get("full_head_channel_offsets", raw["primary_channel_offsets"])),
            tuple(NativeHeadBank(**bank) for bank in raw.get("full_head_banks", raw.get("head_banks", ()))),
            black_only_nozzle_count=(
                int(data["black_only_layout"]["nozzle_count"])
                if "black_only_layout" in data else None),
            channel_nozzle_counts=tuple(
                (int(selector), int(count)) for selector, count in
                data.get("mixed_head_layout", {}).get("nozzles_by_primary_selector", {}).items()
            ),
            black_only_channel_offset=raw.get("black_only_channel_offset"),
        )
        if dict(setup.channel_nozzle_counts).get(0, mode.nozzle_count) > int(raw["primary_nozzle_count"]):
            # A full K bank uses its own measured origin, including in a
            # mixed-ink job. The short colour-mode K window is not the origin
            # of the full bank. Aligning its lower end with CMY moves K by
            # the window-length difference (272 pitches on WF-3620).
            # Keep the native full-K registration instead.
            if setup.black_only_channel_offset is None:
                raise ValueError("Full WorkForce K bank has no registered origin")
            setup = replace(setup, channel_offsets=tuple(
                (selector, setup.black_only_channel_offset if selector == 0 else offset)
                for selector, offset in setup.channel_offsets))
        head_scale = int.from_bytes(setup.resolution_payload[:2], "little")
        pitch = head_scale // setup.resolution_payload[2]
        horizontal = head_scale // setup.resolution_payload[3]
        if (head_scale % setup.resolution_payload[2]
                or head_scale % setup.resolution_payload[3]
                or mode.horizontal_dpi % horizontal
                or mode.vertical_dpi % pitch):
            raise ValueError(f"Non-integral WorkForce raster grid: {mode.identity}")
        result.append(replace(
            mode, nozzle_count=int(raw["primary_nozzle_count"]),
            nozzle_separation=1, base_resolution_dpi=horizontal,
            horizontal_passes=mode.horizontal_dpi // horizontal,
            vertical_passes=max(1, mode.physical_vertical_dpi // mode.vertical_dpi),
            native_raster_setup=setup,
            implemented=bool(raw["command_grid_supported"]),
            offline_validated=bool(raw["command_grid_supported"]
                                   and data.get("offline_geometry_validated", False)),
            blocked_reason=raw.get("blocked_reason"),
        ))
    modes = tuple(result)
    media, aliases = _media(source, modes, package, runtime, ppd)
    media = _matte_media_labels(source, media)
    supported = {identity for item in media for identity in item.supported_modes}
    modes = tuple(mode for mode in modes if mode.identity in supported)
    modes = _workforce_photo_modes(package, modes)
    media = _media_resolution_policy(package, data, modes, media)
    sizes = {item["page_id"]: item for item in source["media_sizes"] if item.get("page_id")}
    pages = []
    for page in ppd.pages:
        size = sizes[page.name]
        mechanics = size["page_mechanics"]
        physical = size["physical_size_mils"]
        pages.append(PageCapability(
            page, True, True, media_size_code=int(mechanics["media_size_code"]),
            sheet_geometry_class=mechanics["sheet_geometry_class"],
            native_page_format_mils=(int(physical[0]), int(physical[1]),
                                     int(size["margins_mils"]["bottom"])),
            physical_media_size_mm=(physical[0] * 0.0254, physical[1] * 0.0254),
        ))
    pages, ppd = _workforce_page_geometry(package, pages, ppd)
    if not modes or not pages:
        raise ValueError("WorkForce package has no usable native modes/pages")
    first = modes[0].native_raster_setup
    assert first is not None
    profile = EpsonEscp2BackendProfile(
        identity="WorkForceNative", units_payload=first.units_payload,
        printhead_resolution_payload=first.resolution_payload,
        rows_per_pass=modes[0].nozzle_count,
        default_dot_size=modes[0].dot_size, default_print_method=None,
        remote_extras=tuple((r["command"], bytes.fromhex(r["payload_hex"]))
                            for r in data["remote_setup"]),
        source="Model-specific ReferenceDriver descriptors and offline native captures",
        emit_paper_path=bool(data["emit_paper_path"]),
        remote_after_paper_path=tuple((r["command"], bytes.fromhex(r["payload_hex"]))
                                     for r in data["remote_after_paper_path"]),
    )
    mechanics = source["driver"]["driver_mechanics"]
    first_offsets = dict(first.channel_offsets)
    paths = tuple(replace(path, head_offset=first_offsets[path.escp2_selector],
                          additional_escp2_selectors=tuple(sorted({
                              bank.selector for mode in modes
                              for bank in mode.native_raster_setup.banks
                              if bank.primary_selector == path.escp2_selector
                              and bank.selector != path.escp2_selector})))
                  for path in _physical_paths(package, source, runtime))
    return PrinterModel(
        identity=package.model_id, display_name=package.display_name,
        backend_family=package.backend_family,
        physical_paths=paths,
        modes=modes, media=media, pages=tuple(pages),
        input_sources=_feeds(source, runtime), ppd=ppd,
        source_summary=(package.package_id, data["technical_descriptor"], *package.warnings),
        media_aliases=aliases, device_settings=_settings(source, runtime),
        mode_aliases=(("pyToneRIP_PhotoFine_2400x2400", PHOTO_FINE_MODE_ID),)
        if package.model_id == "WorkForceWF3620" else (),
        maintenance_enabled=bool(data.get("maintenance_enabled", True)),
        escp2_profile=profile, catalog_package_id=package.package_id,
        command_level=int(mechanics["command_level"]),
        mechanical_feature_set=int(mechanics["mechanical_feature_set"]),
        leading_feed_offset_mils=int(mechanics["leading_feed_offset_mils"]),
        base_separation_dpi=int.from_bytes(first.units_payload[3:], "little") // first.units_payload[1],
    )
