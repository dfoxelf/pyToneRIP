# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Backend-aware validation of a generated XP-750 native stream."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256

from xp750_rip.analysis.escp2_parser import EvidenceClass, StreamRecord, parse_stream
from xp750_rip.escp2.backend import (
    BackendRenderResult, _p700_mode_setup, _et8550_mode_setup, _profile_leading_feed_offset_mils,
)
from xp750_rip.escp2.mode import XP750_MODEL96_MODE_BY_ID
from xp750_rip.printers.xp750 import XP750_PHYSICAL_PATH_BY_ID


@dataclass(frozen=True, slots=True)
class NativeStreamValidation:
    records: tuple[StreamRecord, ...]
    full_stream_consumed: bool
    unknown_record_count: int
    expected_selectors: tuple[int, ...]
    observed_selectors: tuple[int, ...]
    expected_physical_paths: tuple[str, ...]
    observed_physical_paths: tuple[str, ...]
    raster_block_count: int
    packbits_round_trip: bool
    exact_raster_reconstruction: bool
    page_geometry_matches: bool
    normal_termination: bool
    mode_commands_match: bool = True
    media_and_source_match: bool = True
    pass_schedule_matches: bool = True

    @property
    def passed(self) -> bool:
        return all(
            (
                self.full_stream_consumed,
                self.unknown_record_count == 0,
                self.expected_selectors == self.observed_selectors,
                self.expected_physical_paths == self.observed_physical_paths,
                self.packbits_round_trip,
                self.exact_raster_reconstruction,
                self.page_geometry_matches,
                self.mode_commands_match,
                self.media_and_source_match,
                self.pass_schedule_matches,
                self.normal_termination,
            )
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "full_stream_consumed": self.full_stream_consumed,
            "unknown_record_count": self.unknown_record_count,
            "expected_selectors": [f"0x{value:02X}" for value in self.expected_selectors],
            "observed_selectors": [f"0x{value:02X}" for value in self.observed_selectors],
            "expected_physical_paths": list(self.expected_physical_paths),
            "observed_physical_paths": list(self.observed_physical_paths),
            "raster_block_count": self.raster_block_count,
            "packbits_round_trip": self.packbits_round_trip,
            "exact_raster_reconstruction": self.exact_raster_reconstruction,
            "page_geometry_matches": self.page_geometry_matches,
            "mode_commands_match": self.mode_commands_match,
            "media_and_source_match": self.media_and_source_match,
            "pass_schedule_matches": self.pass_schedule_matches,
            "normal_termination": self.normal_termination,
        }

    def failed_checks(self) -> tuple[str, ...]:
        checks = {
            "full_stream_consumed": self.full_stream_consumed,
            "unknown_records": self.unknown_record_count == 0,
            "selectors": self.expected_selectors == self.observed_selectors,
            "physical_paths": self.expected_physical_paths == self.observed_physical_paths,
            "packbits_round_trip": self.packbits_round_trip,
            "exact_raster_reconstruction": self.exact_raster_reconstruction,
            "page_geometry": self.page_geometry_matches,
            "mode_commands": self.mode_commands_match,
            "media_and_source": self.media_and_source_match,
            "pass_schedule": self.pass_schedule_matches,
            "normal_termination": self.normal_termination,
        }
        return tuple(name for name, passed in checks.items() if not passed)


def validate_xp750_native(result: BackendRenderResult) -> NativeStreamValidation:
    records = tuple(parse_stream(result.data))
    full = bool(records) and records[-1].end_offset == len(result.data)
    unknown_count = sum(record.confidence is EvidenceClass.UNKNOWN for record in records)
    raster_records = tuple(record for record in records if record.raster is not None)
    blocks = tuple(block for item in result.job.passes for block in item.blocks)
    expected_selectors = tuple(block.raster.selector for block in blocks)
    observed_selectors = tuple(record.raster.selector for record in raster_records if record.raster)
    expected_paths = tuple(block.path.value for block in blocks)
    selector_to_path = {
        descriptor.address.selector: path.value
        for path, descriptor in XP750_PHYSICAL_PATH_BY_ID.items()
    }
    observed_paths = tuple(selector_to_path.get(selector, "UNKNOWN") for selector in observed_selectors)
    round_trip = len(blocks) == len(raster_records)
    exact = round_trip
    if round_trip:
        for block, record in zip(blocks, raster_records, strict=True):
            parsed = record.raster
            assert parsed is not None
            source = block.raster
            if (
                parsed.compression,
                parsed.bit_depth,
                parsed.bytes_per_row,
                parsed.rows,
            ) != (
                source.compression,
                source.bit_depth,
                source.bytes_per_raster_row,
                source.row_count,
            ):
                exact = False
                round_trip = False
                break
            if parsed.decoded_rows != source.decoded_rows:
                exact = False
            if parsed.compressed_payload != b"".join(source.compressed_rows):
                round_trip = False
            if result.weave_schedule is None:
                expected_plane = result.job.rasters.get(block.path)
                start = block.raster.pass_index * 180
                end = start + block.raster.row_count
                expected_rows = tuple(
                    expected_plane.bit_plane_rows[0][row]
                    + expected_plane.bit_plane_rows[1][row]
                    for row in range(start, end)
                )
            else:
                expected_rows = source.decoded_rows
            if parsed.decoded_rows != expected_rows:
                exact = False

    page_length = next((item for item in records if item.name == "PAGE_LENGTH"), None)
    paper_dimensions = next((item for item in records if item.name == "PAPER_DIMENSIONS"), None)
    page_format = next((item for item in records if item.name == "PAGE_FORMAT"), None)
    page = result.page.ppd
    if result.weave_schedule is None:
        page_units = 180
        expected_top = -180
        expected_bottom_extension = 160
    else:
        mode_parameters = XP750_MODEL96_MODE_BY_ID[result.mode.identity]
        page_units = mode_parameters.page_management_units
        expected_top = -page_units
        expected_bottom_extension = round(page_units * 8 / 9)
    expected_width = round(page.width_points * page_units / 72)
    expected_height = round(page.height_points * page_units / 72)
    page_ok = bool(
        page_length
        and paper_dimensions
        and page_format
        and page_length.parameters.get("value") == expected_height
        and paper_dimensions.parameters.get("width") == expected_width
        and paper_dimensions.parameters.get("height") == expected_height
        and page_format.parameters.get("top") == expected_top
        and page_format.parameters.get("bottom") == expected_height + expected_bottom_extension
    )

    mode = XP750_MODEL96_MODE_BY_ID[result.mode.identity]
    unit_setup = next((item for item in records if item.name == "UNIT_SETUP"), None)
    dot_size = next((item for item in records if item.name == "DOT_SIZE"), None)
    resolution = next((item for item in records if item.name == "RESOLUTION"), None)
    mode_ok = bool(
        unit_setup
        and dot_size
        and resolution
        and unit_setup.parameters.get("base_unit") == mode.unit_scale
        and unit_setup.parameters.get("page_unit")
        == mode.unit_scale // mode.page_management_units
        and unit_setup.parameters.get("vertical_unit")
        == mode.unit_scale // mode.vertical_units
        and unit_setup.parameters.get("horizontal_unit")
        == mode.unit_scale // mode.horizontal_units
        and dot_size.parameters.get("dot_size") == mode.escp2_ink_type
        and resolution.parameters.get("base_unit") == mode.resolution_scale
        and resolution.parameters.get("vertical_unit") == mode.printhead_vertical_unit
        and resolution.parameters.get("horizontal_unit") == mode.printhead_horizontal_unit
    )
    remote_pp = next((item for item in records if item.name == "REMOTE_PP"), None)
    remote_mi = next((item for item in records if item.name == "REMOTE_MI"), None)
    print_method = next((item for item in records if item.name == "PRINT_METHOD"), None)
    media_source_ok = bool(
        remote_pp
        and remote_mi
        and print_method
        and remote_pp.parameters.get("path_type") == result.input_source.path_type
        and remote_pp.parameters.get("path_number") == result.input_source.path_number
        and remote_mi.parameters.get("media_type") == result.medium.media_code
        and print_method.parameters.get("value")
        == result.medium.print_method(result.mode.identity)
    )
    observed_advances = tuple(
        item.parameters.get("value") for item in records if item.name == "RELATIVE_VERTICAL"
    )
    expected_advances = tuple(item.vertical_advance for item in result.job.passes)
    schedule_ok = observed_advances == expected_advances
    tail = [record.name for record in records[-6:]]
    normal_termination = tail == [
        "FORM_FEED",
        "RESET",
        "REMOTE_MODE_ENTER",
        "REMOTE_LD",
        "REMOTE_JE",
        "REMOTE_MODE_EXIT",
    ]
    if not blocks:
        normal_termination = normal_termination and any(item.name == "LINE_FEED" for item in records)

    validation = NativeStreamValidation(
        records=records,
        full_stream_consumed=full,
        unknown_record_count=unknown_count,
        expected_selectors=expected_selectors,
        observed_selectors=observed_selectors,
        expected_physical_paths=expected_paths,
        observed_physical_paths=observed_paths,
        raster_block_count=len(raster_records),
        packbits_round_trip=round_trip,
        exact_raster_reconstruction=exact,
        page_geometry_matches=page_ok,
        mode_commands_match=mode_ok,
        media_and_source_match=media_source_ok,
        pass_schedule_matches=schedule_ok,
        normal_termination=normal_termination,
    )
    if not validation.passed:
        digest = sha256(result.data).hexdigest()
        failed = ", ".join(validation.failed_checks())
        raise ValueError(
            f"Native stream validation FAILED ({failed}; SHA-256 {digest})"
        )
    return validation


def validate_escp2_native(result: BackendRenderResult) -> NativeStreamValidation:
    """Validate a descriptor-driven imported ESC/P2 stream.

    Unlike :func:`validate_xp750_native`, this validator never imports model-96
    channel tables. Expected selectors, units and media commands come from the
    selected ``PrinterModel``. Это не ослабленная проверка: она побайтово
    восстанавливает каждый ESC i raster block и сверяет команды с descriptor.
    """

    profile = result.model.escp2_profile
    if profile is None:
        raise ValueError(f"Printer {result.model.identity} has no ESC/P2 profile")
    records = tuple(parse_stream(result.data))
    full = bool(records) and records[-1].end_offset == len(result.data)
    unknown_count = sum(record.confidence is EvidenceClass.UNKNOWN for record in records)
    raster_records = tuple(record for record in records if record.raster is not None)
    blocks = tuple(block for item in result.job.passes for block in item.blocks)
    expected_selectors = tuple(block.raster.selector for block in blocks)
    observed_selectors = tuple(
        record.raster.selector for record in raster_records if record.raster
    )
    expected_paths = tuple(block.path.value for block in blocks)
    selector_to_path = {
        selector: descriptor.identity.value
        for descriptor in result.model.physical_paths
        for selector in descriptor.escp2_selectors
    }
    observed_paths = tuple(
        selector_to_path.get(selector, "UNKNOWN") for selector in observed_selectors
    )

    round_trip = len(blocks) == len(raster_records)
    exact = round_trip
    if round_trip:
        for block, record in zip(blocks, raster_records, strict=True):
            parsed = record.raster
            assert parsed is not None
            source = block.raster
            if (
                parsed.compression,
                parsed.bit_depth,
                parsed.bytes_per_row,
                parsed.rows,
            ) != (
                source.compression,
                source.bit_depth,
                source.bytes_per_raster_row,
                source.row_count,
            ):
                exact = False
                round_trip = False
                break
            if parsed.decoded_rows != source.decoded_rows:
                exact = False
            if parsed.compressed_payload != b"".join(source.compressed_rows):
                round_trip = False

    unit_setup = next((item for item in records if item.name == "UNIT_SETUP"), None)
    resolution = next((item for item in records if item.name == "RESOLUTION"), None)
    dot_size = next((item for item in records if item.name == "DOT_SIZE"), None)
    if profile.mode_derived_units:
        page_units = result.mode.physical_vertical_dpi
        if (
            result.mode.physical_vertical_dpi < 720
            and result.mode.physical_horizontal_dpi >= 720
        ):
            page_units = 720
        physical_xdpi = min(
            result.mode.base_resolution_dpi,
            result.mode.physical_horizontal_dpi,
        )
        unit_scale = result.model.escp2_unit_scale
        expected_unit = {
            "page_unit": unit_scale // page_units,
            "vertical_unit": unit_scale // page_units,
            "horizontal_unit": unit_scale // result.mode.physical_horizontal_dpi,
            "base_unit": unit_scale,
        }
        expected_resolution = {
            "base_unit": 14400,
            "vertical_unit": (
                14400 // result.mode.physical_vertical_dpi
                if result.mode.escp2_printer_weave_mode is not None
                else 14400 * result.mode.nozzle_separation // result.model.base_separation_dpi
            ),
            "horizontal_unit": 14400 // physical_xdpi,
        }
    else:
        units_payload = profile.units_payload
        head_payload = profile.printhead_resolution_payload
        if profile.identity == "P700RIP":
            units_payload, head_payload, _ = _p700_mode_setup(result.mode)
        elif profile.identity == "ET8550":
            units_payload, head_payload = _et8550_mode_setup(result.mode)
        divisor = units_payload[0]
        scale = int.from_bytes(units_payload[3:5], "little")
        page_units = scale // divisor
        expected_unit = {
            "page_unit": units_payload[0],
            "vertical_unit": units_payload[1],
            "horizontal_unit": units_payload[2],
            "base_unit": scale,
        }
        expected_resolution = {
            "base_unit": int.from_bytes(head_payload[:2], "little"),
            "vertical_unit": head_payload[2],
            "horizontal_unit": head_payload[3],
        }
    expected_dot = (
        result.mode.escp2_dot_size_override
        if result.mode.escp2_dot_size_override is not None
        else result.mode.dot_size
    )
    mode_ok = bool(
        unit_setup
        and resolution
        and dot_size
        and all(unit_setup.parameters.get(key) == value for key, value in expected_unit.items())
        and all(
            resolution.parameters.get(key) == value
            for key, value in expected_resolution.items()
        )
        and dot_size.parameters.get("dot_size") == expected_dot
    )

    page = result.page.ppd
    expected_width = round(page.width_points * page_units / 72)
    expected_height = round(page.height_points * page_units / 72)
    native_format = result.page.native_page_format_mils
    if native_format is not None:
        expected_width = round(native_format[0] * page_units / 1000)
        expected_height = round(native_format[1] * page_units / 1000)
    page_length = next((item for item in records if item.name == "PAGE_LENGTH"), None)
    dimensions = next(
        (item for item in records if item.name == "PAPER_DIMENSIONS"), None
    )
    page_format = next((item for item in records if item.name == "PAGE_FORMAT"), None)
    leading_feed_offset_mils = _profile_leading_feed_offset_mils(result.model)
    expected_top = (
        0
        if result.page.special_media_selector == 0x0201
        else round(page_units * leading_feed_offset_mils / 1000)
    )
    expected_bottom = expected_height + round(page_units * 8 / 9)
    if result.model.escp2_command_set == "Pro":
        expected_top = round((page.height_points - page.imageable_top_points) * page_units / 72)
        expected_bottom = round((page.height_points - page.imageable_bottom_points) * page_units / 72)
    elif native_format is not None:
        expected_top += 1
        expected_bottom = expected_height - round(native_format[2] * page_units / 1000)
        if (profile.identity in {"P700RIP", "ET8550"}
                or result.model.catalog_package_id == "gp--escp2-r2880") \
                and result.input_source.medium_kind == "roll":
            expected_top = (0 if profile.identity == "P700RIP" and page.borderless
                            else round(120 * page_units / 1000))
    elif (
        ((result.model.catalog_package_id or "").startswith("pf--")
         or result.model.catalog_package_id in {"gp--escp2-r2880", "gp--escp2-r3000"})
        and (result.input_source.medium_kind != "roll"
             or result.model.catalog_package_id == "gp--escp2-r2880")
        and not page.borderless
        and result.page.special_media_selector is None
    ):
        expected_bottom = (
            round(page.imageable_top_points * page_units / 72)
            if result.model.catalog_package_id == "pf--Epson_XP980series"
                else expected_height - round(
                    (60 if result.model.catalog_package_id == "gp--escp2-r3000" else 120)
                    * page_units / 1000
                )
        )
        if leading_feed_offset_mils < 0:
            expected_top += 1
        if result.model.catalog_package_id in {
            "pf--Epson_SureColorT3200", "pf--Epson_SureColorT5200",
            "pf--Epson_SureColorT7000", "pf--Epson_SureColorT7200",
        }:
            # Captured ReferenceDriver cut-sheet jobs start ESC (c at 120 mils.
            expected_top = round(120 * page_units / 1000)
        if result.model.catalog_package_id == "gp--escp2-r2880" \
                and result.input_source.medium_kind == "roll":
            expected_top = round(120 * page_units / 1000)
    if (
        (profile.identity in {"ET8550", "P700RIP"}
         or result.model.catalog_package_id == "gp--escp2-r2880")
        and page.borderless
        and result.input_source.medium_kind != "roll"
    ):
        # ET-8500/8550 tpprint cut sheets extend ESC (c by 935/1871 units
        # at 720/1440 page units, respectively.
        if (expected_top < 0 and result.model.catalog_package_id != "gp--escp2-r2880"
                    and profile.identity != "P700RIP"
                and not (profile.identity == "ET8550" and native_format is not None)):
            expected_top += 1
        expected_bottom = expected_height + round(1300 * page_units / 1000) - 1
    page_ok = bool(
        page_length
        and dimensions
        and page_format
        and page_length.parameters.get("value") == expected_height
        and dimensions.parameters.get("width") == expected_width
        and dimensions.parameters.get("height") == expected_height
        and page_format.parameters.get("top") == expected_top
        and page_format.parameters.get("bottom")
        == expected_bottom
    )

    remote_pp = next((item for item in records if item.name == "REMOTE_PP"), None)
    remote_mi = next((item for item in records if item.name == "REMOTE_MI"), None)
    print_method = next((item for item in records if item.name == "PRINT_METHOD"), None)
    expected_method = (
        result.mode.escp2_print_method_override
        if result.mode.escp2_print_method_override is not None
        else result.medium.print_method(result.mode.identity)
        if result.medium.print_methods
        else profile.default_print_method
    )
    method_ok = (
        print_method is None
        if expected_method is None
        else bool(print_method and print_method.parameters.get("value") == expected_method)
    )
    init_commands = (
        () if result.model.catalog_package_id in {"gp--escp2-r2880", "gp--escp2-r3000"}
        else result.input_source.initialization_commands
    )
    path_ok = bool(
        remote_pp
        and remote_pp.parameters.get("path_type") == result.input_source.path_type
        and remote_pp.parameters.get("path_number") == result.input_source.path_number
    )
    if init_commands:
        expected_init = b"".join(
            command.encode("ascii") + len(payload).to_bytes(2, "little") + payload
            for command, payload in init_commands
        )
        path_ok = expected_init in result.data
    media_command_ok = (
        remote_mi is None
        if result.medium.media_code is None
        else bool(
            remote_mi
            and remote_mi.parameters.get("media_type") == result.medium.media_code
            and remote_mi.parameters.get("media_size")
            == (result.page.media_size_code if result.page.media_size_code is not None else 99)
        )
    )
    media_source_ok = bool(path_ok and media_command_ok and method_ok)

    observed_advances = tuple(
        item.parameters.get("value")
        for item in records
        if item.name == "RELATIVE_VERTICAL"
    )
    expected_advances = tuple(item.vertical_advance for item in result.job.passes)
    schedule_ok = observed_advances == expected_advances
    deinit = (
        () if result.model.catalog_package_id in {"gp--escp2-r2880", "gp--escp2-r3000"}
        else result.input_source.deinitialization_commands
    )
    prefix = ([] if result.input_source.suppress_form_feed_after_page else ["FORM_FEED"])
    prefix += ["RESET", "REMOTE_MODE_ENTER"]
    suffix = ["REMOTE_LD", "REMOTE_JE", "REMOTE_MODE_EXIT"]
    tail_records = records[-(len(prefix) + len(deinit) + len(suffix)):]
    tail = [record.name for record in tail_records[:len(prefix)] + tail_records[len(prefix) + len(deinit):]]
    normal_termination = tail == prefix + suffix
    normal_termination = normal_termination and all(
        record.raw == command.encode("ascii") + len(payload).to_bytes(2, "little") + payload
        for record, (command, payload) in zip(tail_records[len(prefix):len(prefix) + len(deinit)], deinit)
    )
    if not blocks:
        normal_termination = normal_termination and any(
            item.name == "LINE_FEED" for item in records
        )

    validation = NativeStreamValidation(
        records=records,
        full_stream_consumed=full,
        unknown_record_count=unknown_count,
        expected_selectors=expected_selectors,
        observed_selectors=observed_selectors,
        expected_physical_paths=expected_paths,
        observed_physical_paths=observed_paths,
        raster_block_count=len(raster_records),
        packbits_round_trip=round_trip,
        exact_raster_reconstruction=exact,
        page_geometry_matches=page_ok,
        mode_commands_match=mode_ok,
        media_and_source_match=media_source_ok,
        pass_schedule_matches=schedule_ok,
        normal_termination=normal_termination,
    )
    if not validation.passed:
        digest = sha256(result.data).hexdigest()
        failed = ", ".join(validation.failed_checks())
        raise ValueError(
            f"Imported ESC/P2 stream validation FAILED ({failed}; SHA-256 {digest})"
        )
    return validation
