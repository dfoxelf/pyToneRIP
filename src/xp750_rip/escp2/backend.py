# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Model/mode-driven facade over the physically verified XP-750 ESC/P2 subset."""

from __future__ import annotations

from dataclasses import dataclass, replace
from hashlib import sha256
from time import perf_counter
from typing import Any, Callable

from xp750_rip.capabilities.ppd import PpdConstraint
from xp750_rip.direction import PrintDirection
from xp750_rip.escp2 import commands
from xp750_rip.escp2.job import (
    GeneratedPhysicalRasterJob,
    MultipathPass,
    MultipathRasterBlock,
    _encode_fixed_model96_stream,
)
from xp750_rip.escp2.mode import XP750_MODEL96_MODE_BY_ID, EpsonEscp2ModeParameters
from xp750_rip.escp2.raster import RasterPass
from xp750_rip.escp2.weave import (
    WeaveSchedule,
    WeaveStageTimings,
    schedule_model96_softweave,
)
from xp750_rip.performance import PerformanceTrace
from xp750_rip.printers.capabilities import (
    InputSourceCapability,
    MediaCapability,
    PageCapability,
    PrinterMode,
    PrinterModel,
)
from xp750_rip.printers.xp750 import (
    XP750_PHASE3C,
    XP750_PHYSICAL_PATH_BY_ID,
    LetterPageGeometry,
)
from xp750_rip.raster import (
    NamedRasterPlane,
    PhysicalRasterSet,
    StreamingPhysicalRasterSet,
)


@dataclass(frozen=True, slots=True)
class BackendRenderResult:
    job: GeneratedPhysicalRasterJob
    model: PrinterModel
    mode: PrinterMode
    medium: MediaCapability
    page: PageCapability
    input_source: InputSourceCapability
    ppd_constraints_checked: tuple[PpdConstraint, ...]
    page_formula_classification: str
    weave_schedule: WeaveSchedule | None = None

    @property
    def data(self) -> bytes:
        return self.job.data

    @property
    def sha256(self) -> str:
        return sha256(self.data).hexdigest()


@dataclass(frozen=True, slots=True)
class BackendStreamResult:
    pass_count: int
    page_formula_classification: str


def _pad_rasters(rasters: PhysicalRasterSet, rows_per_pass: int) -> PhysicalRasterSet:
    padded_height = ((rasters.height_rows + rows_per_pass - 1) // rows_per_pass) * rows_per_pass
    if padded_height == rasters.height_rows:
        return rasters
    row_bytes = rasters.width_pixels // 8
    padding = tuple(bytes(row_bytes) for _ in range(padded_height - rasters.height_rows))
    return PhysicalRasterSet(
        tuple(
            NamedRasterPlane(
                identity=plane.identity,
                width_pixels=plane.width_pixels,
                bit_plane_rows=tuple(bit_plane + padding for bit_plane in plane.bit_plane_rows),
            )
            for plane in rasters.planes
        )
    )


def schedule_xp750_360x180(
    rasters: PhysicalRasterSet,
) -> tuple[PhysicalRasterSet, tuple[MultipathPass, ...]]:
    """Schedule arbitrary-height raster bands using the verified low-mode invariant."""

    if rasters.bit_depth != 2:
        raise ValueError("XP750 360x180sw backend требует двухбитный variable-drop raster")
    unknown = set(rasters.identities) - set(XP750_PHYSICAL_PATH_BY_ID)
    if unknown:
        raise ValueError(
            "XP750 backend не знает PhysicalPaths: "
            + ", ".join(sorted(path.value for path in unknown))
        )
    descriptors = tuple(XP750_PHYSICAL_PATH_BY_ID[path] for path in rasters.identities)
    if any(
        descriptor.effective_head_offset(vertical_dpi=180, base_separation_dpi=360)
        for descriptor in descriptors
    ):
        raise ValueError("360x180sw invariant effective head offset == 0 нарушен")

    padded = _pad_rasters(rasters, XP750_PHASE3C.rows_per_pass)
    ordered = tuple(
        sorted(
            padded.planes,
            key=lambda plane: XP750_PHYSICAL_PATH_BY_ID[
                plane.identity
            ].gutenprint_allocation_index,
        )
    )
    passes: list[MultipathPass] = []
    previous_band = -1
    for band_index, start in enumerate(range(0, padded.height_rows, 180)):
        end = start + 180
        blocks: list[MultipathRasterBlock] = []
        for plane in ordered:
            sliced = tuple(bit_plane[start:end] for bit_plane in plane.bit_plane_rows)
            if not any(any(row) for bit_plane in sliced for row in bit_plane):
                continue
            descriptor = XP750_PHYSICAL_PATH_BY_ID[plane.identity]
            advance = (band_index - previous_band) * 180
            raster = RasterPass(
                pass_index=band_index,
                vertical_advance=advance,
                raster_width_pixels=padded.width_pixels,
                plane0_rows=sliced[0],
                plane1_rows=sliced[1],
                address=descriptor.address,
            )
            blocks.append(MultipathRasterBlock(plane.identity, raster))
        if blocks:
            advance = (band_index - previous_band) * 180
            passes.append(MultipathPass(band_index, advance, tuple(blocks)))
            previous_band = band_index
    return padded, tuple(passes)


def _page_geometry(page: PageCapability) -> LetterPageGeometry:
    ppd = page.ppd
    width_units = round(ppd.width_points * 180 / 72)
    height_units = round(ppd.height_points * 180 / 72)
    # FACT for the physically verified Letter stream.  Reuse for other bordered
    # PPD sizes is a declared source-derived inference pending physical checks.
    return LetterPageGeometry(
        name=ppd.name,
        width_inches=ppd.width_points / 72,
        height_inches=ppd.height_points / 72,
        width_page_units=width_units,
        height_page_units=height_units,
        format_top_vertical_units=-180,
        format_bottom_vertical_units=height_units + 160,
    )


@dataclass(frozen=True, slots=True)
class GeneratedModeRasterJob:
    """Mode-aware native job while retaining the generic routed rasters."""

    data: bytes
    rasters: PhysicalRasterSet | StreamingPhysicalRasterSet
    passes: tuple[Any, ...]

    @property
    def active_paths(self) -> tuple[Any, ...]:
        return self.rasters.identities


def _mode_page_geometry(
    page: PageCapability,
    page_units: int,
    *,
    leading_feed_offset_mils: int | None = None,
    use_imageable_margins: bool = False,
    use_imageable_bottom: bool = False,
    bottom_margin_mils: int | None = None,
    top_margin_mils: int | None = None,
    borderless_bottom_extension_mils: int | None = None,
    borderless_top_extra_unit: bool = True,
) -> LetterPageGeometry:
    ppd = page.ppd
    width = round(ppd.width_points * page_units / 72)
    height = round(ppd.height_points * page_units / 72)
    native_format = page.native_page_format_mils
    if native_format is not None:
        width_mils, height_mils, _ = native_format
        width = round(width_mils * page_units / 1000)
        height = round(height_mils * page_units / 1000)
    # Model-96 uses the historically verified one-page-unit negative top.
    # Imported models pass an explicit signed 1/1000-inch feed offset; zero is
    # meaningful there and must not inherit the model-96 default.
    # Model-96 использует проверенный отрицательный top в одну единицу страницы.
    # Импортированные модели всегда передают явное смещение в 1/1000 дюйма.
    # Верхняя подача — знаковое значение в тысячных долях дюйма; это не margin
    # GUI и не цветовая коррекция.
    top = (
        -page_units
        if leading_feed_offset_mils is None
        else 0
        if page.special_media_selector == 0x0201
        else round(page_units * leading_feed_offset_mils / 1000)
    )
    bottom = height + round(page_units * 8 / 9)
    if use_imageable_margins:
        top = round((ppd.height_points - ppd.imageable_top_points) * page_units / 72)
        bottom = round((ppd.height_points - ppd.imageable_bottom_points) * page_units / 72)
    elif native_format is not None:
        # ReferenceDriver cut-sheet captures use an inclusive negative top and the
        # sheet's trailing printable limit, not model-96's extended bottom.
        top += 1
        bottom_mils = native_format[2]
        bottom = height - round(bottom_mils * page_units / 1000)
    elif use_imageable_bottom:
        # ReferenceDriver cut-sheet streams end ESC (c at the imageable bottom. The
        # legacy model-96 extension would put this limit beyond the sheet.
        bottom = (
            height - round(bottom_margin_mils * page_units / 1000)
            if bottom_margin_mils is not None
            else round(ppd.imageable_top_points * page_units / 72)
        )
        if leading_feed_offset_mils is not None and leading_feed_offset_mils < 0:
            top += 1
    if top_margin_mils is not None:
        top = round(top_margin_mils * page_units / 1000)
    if ppd.borderless and borderless_bottom_extension_mils is not None:
        if top < 0 and borderless_top_extra_unit:
            top += 1
        bottom = height + round(borderless_bottom_extension_mils * page_units / 1000) - 1
    return LetterPageGeometry(
        name=ppd.name,
        width_inches=ppd.width_points / 72,
        height_inches=ppd.height_points / 72,
        width_page_units=width,
        height_page_units=height,
        format_top_vertical_units=top,
        format_bottom_vertical_units=bottom,
    )


def _profile_imageable_origin_offsets(
    model: PrinterModel, page: PageCapability, native_mode: EpsonEscp2ModeParameters,
    *, input_source: InputSourceCapability | None = None,
) -> tuple[int, int]:
    """Locate the cropped PPD imageable raster on the physical sheet."""

    ppd = page.ppd
    left = round(ppd.imageable_left_points * native_mode.horizontal_units / 72)
    if model.escp2_command_set == "Pro":
        # Pro profiles place the top margin in ESC (c page format.
        return 0, left
    if not model.raster_origin_at_page_edge:
        return 0, 0
    top_points = ppd.height_points - ppd.imageable_top_points
    top = round(top_points * native_mode.printed_vertical_dpi / 72)
    if model.catalog_package_id == "pf--Epson_SureColorP900_RIP":
        # P900 native X is relative to the printable area, as in independent
        # ReferenceDriver captures. The Windows page mapper has already
        # removed the left non-printable strip; adding it here shifts twice.
        left = 0
        if input_source is not None and input_source.medium_kind == "roll":
            top = 0
    return top, left


def _profile_initial_feed_offset_rows(
    model: PrinterModel, native_mode: EpsonEscp2ModeParameters,
    *, page: PageCapability | None = None,
    input_source: InputSourceCapability | None = None,
) -> int:
    """Place the first host-weave row at the printable head position.

    ReferenceDriver's ET-2550 and desktop SureColor P streams start with a full nozzle-height
    raster block whose first marked rows are at its trailing end. Our compact
    host-weave blocks start with nozzle zero, so their feed origin needs the
    corresponding head span. This is a paper-feed coordinate adjustment, not
    an offset between colour channels.
    """

    if model.catalog_package_id == "gp--escp2-r2880":
        if page is None or input_source is None:
            raise ValueError("R2880 feed origin requires the page and physical feed")
        if input_source.medium_kind == "roll":
            return 0
        # Independent R2880 captures locate the common channel origin one
        # command unit and one 360-dpi stagger row before 1120 mils on sheets,
        # or 900 mils on borderless sheets. Channel head offsets stay separate.
        origin_mils = 900 if page.ppd.borderless else 1120
        units = native_mode.vertical_units
        origin = round(origin_mils * units / 1000) - 1
        stagger = round(units / model.base_separation_dpi)
        return round((origin - stagger) * native_mode.printed_vertical_dpi / units)
    if model.catalog_package_id == "pf--Epson_SureColorP900_RIP":
        if page is None or input_source is None:
            raise ValueError("P900 feed origin requires the page and physical feed")
        if input_source.medium_kind == "roll":
            return 0
        if page.ppd.borderless:
            # Captured P900 borderless sheets start one command unit before
            # 900 mils. Ordinary sheets retain their existing head-span setup.
            units = native_mode.vertical_units
            origin = round(900 * units / 1000) - 1
            return round(origin * native_mode.printed_vertical_dpi / units)
    if model.catalog_package_id not in {
        "pf--Epson_EcoTank_ET2550",
        "pf--Epson_SureColorP400",
        "pf--Epson_SureColorP600",
        "pf--Epson_SureColorP700_RIP",
        "pf--Epson_SureColorP800",
        "pf--Epson_SureColorP900_RIP",
        "pf--Epson_SureColorF500",
        "pf--Epson_SureColorF100",
        "pf--Epson_SureColorT3100",
        "pf--Epson_SureColorT3100X",
        "pf--Epson_SureColorT5100",
    }:
        return 0
    # The scheduler's first_line uses printed raster rows. Convert the
    # physical nozzle pitch to that grid; physical_vertical_dpi can be larger
    # when a ReferenceDriver mode has multiple vertical passes (F500 photo modes).
    return round(
        (native_mode.nozzle_count - 1)
        * native_mode.raw_nozzle_separation
        * native_mode.printed_vertical_dpi
        / native_mode.base_separation_dpi
    )


def _profile_uses_imageable_bottom(
    model: PrinterModel, page: PageCapability, input_source: InputSourceCapability,
) -> bool:
    if model.catalog_package_id in {"gp--escp2-r2880", "gp--escp2-r3000"}:
        return not page.ppd.borderless and page.special_media_selector is None
    return (
        (model.catalog_package_id or "").startswith("pf--")
        and input_source.medium_kind != "roll"
        and not page.ppd.borderless
        and page.special_media_selector is None
    )


def _profile_bottom_margin_mils(model: PrinterModel) -> int | None:
    # All 101 catalogued ReferenceDriver Epson models with an installed technical
    # .tpp specify 120 mils for ordinary cut sheets. XP980 has no installed
    # .tpp here, so use its PPD imageable area instead.
    package_id = model.catalog_package_id or ""
    if package_id == "gp--escp2-r2880":
        return 120
    if package_id == "gp--escp2-r3000":
        # Captured R3000 custom sheet: 15840 - 15667 units at 2880/in.
        return 60
    if package_id.startswith("pf--") and package_id != "pf--Epson_XP980series":
        return 120
    return None


def _profile_borderless_bottom_extension_mils(
    model: PrinterModel, input_source: InputSourceCapability,
) -> int | None:
    # Captured sheet limits extend by 935 units at 720/in and 1871 at 1440/in.
    if (
        model.escp2_profile is not None
        and (model.escp2_profile.identity in {"ET8550", "P700RIP"}
             or model.catalog_package_id == "gp--escp2-r2880")
        and input_source.medium_kind != "roll"
    ):
        return 1300
    return None


def _profile_top_margin_mils(
    model: PrinterModel, page: PageCapability, input_source: InputSourceCapability,
) -> int | None:
    if (model.escp2_profile is not None and model.escp2_profile.identity == "ET8550"
            and input_source.medium_kind == "roll"):
        return 120
    if (
        model.catalog_package_id in {
            "pf--Epson_SureColorP700_RIP",
            "pf--Epson_SureColorP900_RIP",
            "gp--escp2-r2880",
        }
        and input_source.medium_kind == "roll"
        and (not page.ppd.borderless or model.escp2_profile.identity == "P700RIP")
    ):
        # ReferenceDriver P700/P900 roll captures use a 120-mil positive top,
        # while their cut sheets use the model's negative feed origin.
        return 0 if page.ppd.borderless else 120
    # ReferenceDriver T7000 sheet captures use a 120-mil top in ESC (c. Its synthetic
    # PPD's 9-point imageable inset is only an approximation of this value.
    if (
        model.catalog_package_id == "pf--Epson_SureColorT7000"
        and _profile_uses_imageable_bottom(model, page, input_source)
    ):
        return 120
    if (
        model.catalog_package_id in {
            "pf--Epson_SureColorT3200", "pf--Epson_SureColorT5200",
            "pf--Epson_SureColorT7200",
        }
        and _profile_uses_imageable_bottom(model, page, input_source)
    ):
        # The T3200 Letter reference starts ESC (c at 173/1440 in = 120 mil.
        return 120
    return None


def _profile_leading_feed_offset_mils(model: PrinterModel) -> int:
    if model.catalog_package_id == "gp--escp2-r2880":
        # Captured R2880 ReferenceDriver sheets use the same -1-inch head origin.
        return -1000
    if model.catalog_package_id == "gp--escp2-r3000":
        # Captured R3000 sheet starts 1439 units above zero at 2880/in.
        return -500
    return model.leading_feed_offset_mils if model.command_level is not None else 0


_PAPER_MEDIA_SIZE_BY_PAGE = {
    # Gutenprint's escp2/mediasizes/standard.xml values.  This is the fourth
    # byte of REMOTE1 MI and is separate from both the physical dimensions
    # sent by ESC (C and the paper-path selection sent by REMOTE1 PP.
    "A4": 0,
    "TA4": 0,
    "4X6FULL": 10,
    "T4X6FULL": 10,
    "2L": 28,
    "T2L": 28,
    "A3": 62,
    "TA3": 62,
    "B5": 5,
    "L": 15,
    "TL": 15,
    "INDEX5": 11,
    "8x10": 12,
    "T8x10": 12,
    # These PPD sizes do not have a named entry in Gutenprint's standard
    # MediaSize table.  Its documented default is 99, which tells the printer
    # to use the physical dimensions carried by the following ESC/P2 commands.
    "4X7": 99,
    "T4X7": 99,
    "Postcard": 99,
    "TPostcard": 99,
    "ENV10": 29,
    "ENVC6": 30,
    "EnvDL": 31,
    "Letter": 1,
    "TLetter": 1,
    "Legal": 2,
    "A5": 3,
    "A6": 4,
}


def _paper_media_size(page: PageCapability) -> int:
    """Return the model96 REMOTE1 ``PaperMediaSize`` for a PPD page.

    Every PageSize in the XP-750/XP-960 PPD is listed deliberately.  A missing
    entry is a programming error: silently sending Letter (the former fixed
    value ``1``) would select an incorrect feed path for small media.
    """

    try:
        return _PAPER_MEDIA_SIZE_BY_PAGE[page.ppd.name]
    except KeyError as error:
        raise ValueError(
            "No REMOTE1 PaperMediaSize mapping for PageSize=" + page.ppd.name
        ) from error


def _roll_cut_page_height(page: PageCapability, base_separation_dpi: int) -> int:
    """Convert page height to the coordinate used by REMOTE1 CO.

    Epson's ESC/P2 implementation defines the CO height as
    ``page_points * 5 / (base_separation / 360)``. Keep this formula explicit:
    the value is neither application DPI nor printer raster DPI.

    Высота CO измеряется отдельно от координат растра и страницы. Явная
    формула не позволяет ошибочно подставить сюда DPI выбранного режима.
    """

    if base_separation_dpi < 360 or base_separation_dpi % 360:
        raise ValueError(
            "Roll cutting requires base separation divisible by 360 DPI"
        )
    divisor = base_separation_dpi // 360
    return round(page.ppd.height_points * 5 / divisor)


def _append_roll_mechanics(
    output: bytearray,
    *,
    input_source: InputSourceCapability,
    page: PageCapability,
    base_separation_dpi: int,
) -> None:
    if input_source.cutter_edge_trim_mode is not None and not any(
        command == "SN" and payload[:2] == b"\x00\x84"
        for command, payload in input_source.initialization_commands
    ):
        output.extend(
            commands.remote_cutter_edge_trim(input_source.cutter_edge_trim_mode)
        )
    if input_source.roll_cut_mode == "none":
        return
    if input_source.medium_kind != "roll":
        raise ValueError("A non-roll input source cannot request a cutter operation")
    output.extend(
        commands.remote_roll_cut(
            mode=input_source.roll_cut_mode,
            page_height=_roll_cut_page_height(page, base_separation_dpi),
        )
    )


def _encode_mode96_stream(
    *,
    mode_id: str,
    medium: MediaCapability,
    page: PageCapability,
    input_source: InputSourceCapability,
    geometry: LetterPageGeometry,
    encoded_passes: tuple[bytes, ...],
    job_label: str,
    direction: PrintDirection,
    remote_setup_commands: tuple[bytes, ...] = (),
    split_parts: bool = False,
) -> bytes | tuple[bytes, bytes]:
    mode = XP750_MODEL96_MODE_BY_ID[mode_id]
    if medium.media_code is None:
        raise ValueError(f"Media {medium.identity} не имеет PaperMedia code")
    if input_source.path_type is None or input_source.path_number is None:
        raise ValueError(f"Input source {input_source.identity} не имеет REMOTE1 PP mapping")
    output = bytearray(commands.ejl_packet_mode_exit())
    output.extend(commands.reset())
    output.extend(commands.reset())
    output.extend(commands.remote_mode_enter())
    output.extend(commands.remote_job_start(job_label))
    output.extend(commands.remote_sn(0))
    output.extend(
        commands.remote_paper_path(
            path_type=input_source.path_type, path_number=input_source.path_number
        )
    )
    output.extend(
        commands.remote_media_info(
            media_format=1,
            media_type=medium.media_code,
            media_size=_paper_media_size(page),
        )
    )
    for device_command in remote_setup_commands:
        output.extend(device_command)
    _append_roll_mechanics(
        output,
        input_source=input_source,
        page=page,
        base_separation_dpi=360,
    )
    output.extend(commands.remote_mode_exit())
    output.extend(commands.graphics_mode())
    output.extend(
        commands.units(
            page=mode.page_management_units,
            vertical=mode.vertical_units,
            horizontal=mode.horizontal_units,
            scale=mode.unit_scale,
        )
    )
    output.extend(commands.gray_mode(2))
    output.extend(commands.microweave(0))
    bidirectional = direction is PrintDirection.BIDIRECTIONAL
    output.extend(commands.unidirectional(not bidirectional))
    output.extend(commands.dot_size(mode.escp2_ink_type))
    output.extend(
        commands.printhead_resolution(
            scale=mode.resolution_scale,
            vertical=mode.printhead_vertical_unit,
            horizontal=mode.printhead_horizontal_unit,
        )
    )
    output.extend(commands.page_length(geometry.height_page_units))
    output.extend(
        commands.page_format(
            geometry.format_top_vertical_units,
            geometry.format_bottom_vertical_units,
        )
    )
    output.extend(
        commands.paper_dimensions(geometry.width_page_units, geometry.height_page_units)
    )
    output.extend(commands.print_method(medium.print_method(mode_id)))
    prefix_end = len(output)
    for item in encoded_passes:
        output.extend(item)
    if not encoded_passes and not split_parts:
        output.extend(commands.line_feed())
    # A small group of roll paths finishes the page by flushing the last raster
    # band and resetting the job, without sending FF (0x0C). This is a property
    # of the selected physical feed path, not of image color processing.
    # Некоторые рулонные тракты завершают страницу без FF; признак относится к
    # механике подачи и поэтому хранится в InputSourceCapability.
    if not input_source.suppress_form_feed_after_page:
        output.extend(commands.form_feed())
    output.extend(commands.reset())
    output.extend(commands.remote_mode_enter())
    output.extend(commands.remote_load_defaults())
    output.extend(commands.remote_job_end(0))
    output.extend(commands.remote_mode_exit())
    if split_parts:
        return bytes(output[:prefix_end]), bytes(output[prefix_end:])
    return bytes(output)


def _p700_mode_setup(mode: PrinterMode) -> tuple[bytes, bytes, int]:
    """ReferenceDriver P700/P900 RIP ESC (U / ESC (D for the five native qualities."""

    if mode.physical_vertical_dpi >= 1440:
        return bytes.fromhex("02 02 01 40 0b"), bytes.fromhex("40 0b 10 04"), 1440
    if mode.physical_horizontal_dpi >= 1440:
        return bytes.fromhex("02 02 01 a0 05"), bytes.fromhex("a0 05 08 02"), 720
    if mode.physical_horizontal_dpi >= 720:
        return bytes.fromhex("02 02 02 a0 05"), bytes.fromhex("a0 05 08 02"), 720
    return bytes.fromhex("02 02 02 a0 05"), bytes.fromhex("a0 05 08 04"), 720


def _et8550_mode_setup(mode: PrinterMode) -> tuple[bytes, bytes]:
    # ET-8550 captures for all four qualities of plain, gloss and film.
    # Command coordinates differ from application and declared printer DPI.
    horizontal = max(720, mode.physical_horizontal_dpi,
                     2880 if mode.physical_horizontal_dpi >= 1440 else 0)
    vertical = max(720, mode.physical_vertical_dpi)
    scale = max(1440, horizontal)
    units = bytes((scale // vertical, scale // vertical, scale // horizontal)) + scale.to_bytes(2, "little")
    head = scale.to_bytes(2, "little") + bytes((scale // 180, scale // min(720, mode.horizontal_dpi)))
    return units, head


def _profile_mode(mode: PrinterMode, model: PrinterModel) -> EpsonEscp2ModeParameters:
    """Translate normalized capabilities into the reusable weave mechanics."""

    profile = model.escp2_profile
    command_vertical_units = None
    command_horizontal_units = None
    if profile is not None and profile.identity == "P700RIP":
        units = _p700_mode_setup(mode)[0]
        scale = int.from_bytes(units[3:5], "little")
        command_vertical_units = scale // units[1]
        command_horizontal_units = scale // units[2]
    elif profile is not None and profile.identity == "ET8550":
        payload = _et8550_mode_setup(mode)[0]
        scale = int.from_bytes(payload[3:5], "little")
        command_vertical_units = scale // payload[1]
        command_horizontal_units = scale // payload[2]
    elif profile is not None and not profile.mode_derived_units:
        payload = profile.units_payload
        divisor = payload[1]
        scale = int.from_bytes(payload[3:5], "little")
        if divisor == 0 or scale == 0 or scale % divisor:
            raise ValueError(f"Invalid fixed ESC (U vertical unit for {model.identity}")
        command_vertical_units = scale // divisor
    return EpsonEscp2ModeParameters(
        identity=mode.identity,
        physical_horizontal_dpi=mode.physical_horizontal_dpi,
        physical_vertical_dpi=mode.physical_vertical_dpi,
        printed_horizontal_dpi=mode.horizontal_dpi,
        printed_vertical_dpi=mode.vertical_dpi,
        base_resolution_dpi=mode.base_resolution_dpi,
        vertical_passes=mode.vertical_passes,
        density=mode.density,
        escp2_ink_type=mode.escp2_ink_type,
        bit_depth=mode.bit_depth,
        drop_sizes=mode.drop_sizes,
        ordered_matrix_id=mode.ordered_matrix_id,
        nozzle_count=(1 if mode.escp2_printer_weave_mode is not None else mode.nozzle_count),
        raw_nozzle_separation=(
            1 if mode.escp2_printer_weave_mode is not None else mode.nozzle_separation
        ),
        printer_weave_mode=mode.escp2_printer_weave_mode,
        unit_scale=model.escp2_unit_scale,
        base_separation_dpi=model.base_separation_dpi,
        command_vertical_units=command_vertical_units,
        command_horizontal_units=command_horizontal_units,
        # P700/P900 Super consumes 720 dpi rows while its command grid is
        # 1440 dpi. Express nozzle pitch and head offsets in the source-row
        # grid; ESC (U / ESC (D retain their independently captured values.
        weave_vertical_dpi=(
            mode.vertical_dpi
            if profile is not None and profile.identity == "P700RIP"
            else None
        ),
    )


def _profile_page_units(model: PrinterModel, mode: EpsonEscp2ModeParameters) -> int:
    profile = model.escp2_profile
    assert profile is not None
    if profile.identity == "P700RIP":
        return mode.page_management_units
    if profile.identity == "ET8550":
        return mode.page_management_units
    if profile.mode_derived_units:
        return mode.page_management_units
    divisor = profile.units_payload[0]
    scale = int.from_bytes(profile.units_payload[3:5], "little")
    if divisor == 0 or scale == 0:
        raise ValueError(f"Invalid ESC (U payload for {model.identity}")
    return scale // divisor


def _selected_device_commands(
    model: PrinterModel, selected: dict[str, str] | None, *, skip: tuple[str, ...] = ()
) -> tuple[bytes, ...]:
    values = selected or {}
    unknown = set(values) - {item.identity for item in model.device_settings}
    if unknown:
        raise ValueError("Unknown device settings: " + ", ".join(sorted(unknown)))
    commands_to_send: list[bytes] = []
    for setting in model.device_settings:
        if setting.identity in skip:
            continue
        if ((model.catalog_package_id in {"gp--escp2-r2400", "gp--escp2-r2880"}
             or model.backend_family == "epson_escp2_model96")
            and setting.identity == "Drying_Time" and setting.identity not in values):
            # Legacy jobs did not advertise a drying override. An explicit
            # selection, including Off, sends DR; unchanged jobs keep their header.
            continue
        choice = setting.choice(values.get(setting.identity, setting.default_choice))
        if choice.remote_command:
            commands_to_send.append(
                commands.remote_payload(choice.remote_command, choice.remote_payload)
            )
    return tuple(commands_to_send)


def _append_p700_remote_setup(
    output: bytearray,
    model: PrinterModel,
    page: PageCapability,
    input_source: InputSourceCapability,
    selected: dict[str, str] | None,
) -> None:
    """Emit the ReferenceDriver P700/P900 direct-channel REMOTE1 setup order."""

    values = selected or {}
    settings = {setting.identity: setting for setting in model.device_settings}
    unknown = set(values) - set(settings)
    if unknown:
        raise ValueError("Unknown device settings: " + ", ".join(sorted(unknown)))

    def payload(name: str) -> bytes:
        setting = settings[name]
        return setting.choice(values.get(name, setting.default_choice)).remote_payload

    platen = payload("Platen_Gap")
    output.extend(commands.remote_payload("DP", b"\x00\x00"))
    borderless_sheet = page.ppd.borderless and input_source.medium_kind != "roll"
    output.extend(commands.remote_payload("US", bytes((0, 0, 2 if borderless_sheet else 1))))
    output.extend(commands.remote_payload("US", platen))
    output.extend(commands.remote_payload("US", b"\x00\x02\x00"))
    output.extend(commands.remote_payload("FP", bytes.fromhex("00b0ff" if page.ppd.borderless else "000000")))
    output.extend(commands.remote_payload(
        "EX", b"\x00\x00\x00\x00\x05" +
        (b"\x01" if input_source.medium_kind == "roll" else b"\x00"),
    ))
    if input_source.initialization_commands:
        for remote_command, feed_payload in input_source.initialization_commands:
            output.extend(commands.remote_payload(remote_command, feed_payload))
    else:
        output.extend(commands.remote_paper_path(
            path_type=input_source.path_type,
            path_number=input_source.path_number,
        ))
    output.extend(commands.remote_payload("DR", payload("Drying_Time")))
    page_drying = settings["Page_Drying_Time"]
    if values.get(page_drying.identity, page_drying.default_choice) != page_drying.default_choice:
        output.extend(commands.remote_payload("DR", payload("Page_Drying_Time")))
    output.extend(commands.remote_payload("PH", payload("Paper_Thickness")))
    output.extend(commands.remote_payload("AC", (
        payload("Save_Paper_Roll_Paper") if "Save_Paper_Roll_Paper" in settings else b"\x00\x40"
    )))
    output.extend(commands.remote_payload("US", platen))


def _append_et8550_remote_setup(
    output: bytearray, model: PrinterModel, page: PageCapability,
    input_source: InputSourceCapability, selected: dict[str, str] | None,
) -> None:
    # The borderless FP offset and continuous EX flag were captured explicitly.
    platen_commands = _selected_device_commands(model, selected)
    output.extend(commands.remote_payload("DP", bytes.fromhex("0000")))
    output.extend(commands.remote_payload("US", bytes((0, 0, 2 if page.ppd.borderless else 1))))
    output.extend(commands.remote_payload("US", bytes.fromhex("000100")))
    output.extend(commands.remote_payload("US", bytes.fromhex("000200")))
    output.extend(commands.remote_payload("FP", bytes.fromhex("00b0ff" if page.ppd.borderless else "000000")))
    output.extend(commands.remote_payload("EX", bytes((0, 0, 0, 0, 5,
                   1 if input_source.medium_kind == "roll" else 0))))
    output.extend(commands.remote_paper_path(path_type=input_source.path_type,
                                           path_number=input_source.path_number))
    for device_command in platen_commands:
        output.extend(device_command)


def _append_r2880_remote_setup(
    output: bytearray, model: PrinterModel, page: PageCapability,
    input_source: InputSourceCapability,
    selected: dict[str, str] | None,
) -> None:
    """ReferenceDriver R2880 initialization verified for sheet and roll paths."""

    settings = {setting.identity: setting for setting in model.device_settings}
    values = selected or {}
    unknown = set(values) - set(settings)
    if unknown:
        raise ValueError("Unknown device settings: " + ", ".join(sorted(unknown)))
    platen = settings["Platen_Gap"]
    platen_payload = platen.choice(
        values.get(platen.identity, platen.default_choice)
    ).remote_payload
    output.extend(commands.remote_payload("DP", b"\x00\x00"))
    borderless = model.catalog_package_id == "gp--escp2-r2880" and page.ppd.borderless
    output.extend(commands.remote_payload("US", bytes((0, 0, 2 if borderless else 1))))
    output.extend(commands.remote_payload("US", platen_payload))
    output.extend(commands.remote_payload("US", b"\x00\x02\x00"))
    output.extend(commands.remote_payload("FP", bytes.fromhex("00b0ff" if borderless else "000000")))
    output.extend(commands.remote_payload(
        "EX", b"\x00\x00\x00\x00\x05" +
        (b"\x01" if input_source.medium_kind == "roll" else b"\x00"),
    ))
    output.extend(commands.remote_paper_path(
        path_type=input_source.path_type,
        path_number=input_source.path_number,
    ))
    for device_command in _selected_device_commands(model, selected, skip=("Platen_Gap",)):
        output.extend(device_command)


def _medium_mechanical_commands(medium: MediaCapability) -> tuple[bytes, ...]:
    """Encode only mechanics explicitly present on the selected medium.

    Missing values mean that no command is sent; zero is a real protocol
    value and must not be confused with absence. / Отсутствующее поле не
    посылается, а ноль является полноценным значением команды.
    """

    encoded: list[bytes] = []
    if medium.feed_adjustment_code is not None:
        encoded.append(commands.remote_feed_adjustment(medium.feed_adjustment_code))
    if medium.vacuum_intensity_code is not None:
        encoded.append(commands.remote_vacuum_intensity(medium.vacuum_intensity_code))
    if medium.paper_thickness_code is not None:
        encoded.append(commands.remote_paper_thickness(medium.paper_thickness_code))
    if medium.feed_sequence_code is not None:
        encoded.append(commands.remote_feed_sequence(medium.feed_sequence_code))
    if medium.platen_gap_code is not None:
        encoded.append(commands.remote_platen_gap(medium.platen_gap_code))
    return tuple(encoded)


def _use_alternate_print_method(
    model: PrinterModel, selected: dict[str, str] | None
) -> bool:
    """Return whether a local option requests the mode's alternate ESC (m value."""

    values = selected or {}
    return any(
        setting.choice(values.get(setting.identity, setting.default_choice)).use_alternate_print_method
        for setting in model.device_settings
    )


def resolve_profile_print_method(
    model: PrinterModel,
    mode: PrinterMode,
    medium: MediaCapability,
    device_settings: dict[str, str] | None = None,
) -> int | None:
    """Resolve ESC (m exactly as encoded; None means the command is absent."""

    profile = model.escp2_profile
    assert profile is not None
    medium_print_method = dict(medium.print_methods).get(mode.identity)
    return (
        mode.escp2_alternate_print_method
        if _use_alternate_print_method(model, device_settings)
        and mode.escp2_alternate_print_method is not None
        else mode.escp2_print_method_override
        if mode.escp2_print_method_override is not None
        else medium_print_method
        if medium_print_method is not None
        else profile.default_print_method
    )


def _encode_profile_stream(
    *,
    model: PrinterModel,
    mode: PrinterMode,
    medium: MediaCapability,
    page: PageCapability,
    input_source: InputSourceCapability,
    geometry: LetterPageGeometry,
    encoded_passes: tuple[bytes, ...],
    job_label: str,
    direction: PrintDirection,
    device_settings: dict[str, str] | None,
    ink_configuration: str | None = None,
    split_parts: bool = False,
) -> bytes | tuple[bytes, bytes]:
    profile = model.escp2_profile
    if profile is None:
        raise ValueError(f"Printer {model.identity} has no ESC/P2 profile")
    if input_source.path_type is None or input_source.path_number is None:
        raise ValueError(f"Input source {input_source.identity} has no REMOTE1 PP mapping")
    output = bytearray(commands.ejl_packet_mode_exit())
    output.extend(commands.reset())
    output.extend(commands.reset())
    output.extend(commands.remote_mode_enter())
    output.extend(commands.remote_job_start(job_label))
    output.extend(commands.remote_sn(0))
    if medium.media_code is not None:
        output.extend(
            commands.remote_media_info(
                media_format=1,
                media_type=medium.media_code,
                media_size=page.media_size_code if page.media_size_code is not None else 99,
            )
        )
    for medium_command in _medium_mechanical_commands(medium):
        output.extend(medium_command)
    if profile.identity == "P700RIP":
        _append_p700_remote_setup(output, model, page, input_source, device_settings)
    elif profile.identity == "ET8550":
        _append_et8550_remote_setup(output, model, page, input_source, device_settings)
    elif model.catalog_package_id in {"gp--escp2-r2880", "gp--escp2-r3000"}:
        _append_r2880_remote_setup(output, model, page, input_source, device_settings)
    else:
        for remote_command, payload in profile.remote_extras:
            output.extend(commands.remote_payload(remote_command, payload))
        for device_command in _selected_device_commands(
            model, device_settings, skip=("Save_Paper_Roll_Paper",)
        ):
            output.extend(device_command)
        if input_source.initialization_commands:
            for remote_command, payload in input_source.initialization_commands:
                output.extend(commands.remote_payload(remote_command, payload))
        else:
            output.extend(
                commands.remote_paper_path(
                    path_type=input_source.path_type,
                    path_number=input_source.path_number,
                )
            )
        save_roll = next((setting for setting in model.device_settings
                          if setting.identity == "Save_Paper_Roll_Paper"), None)
        if save_roll is not None:
            choice = save_roll.choice((device_settings or {}).get(
                save_roll.identity, save_roll.default_choice))
            # Captured AC is applied after the physical PP path selection.
            output.extend(commands.remote_payload(choice.remote_command, choice.remote_payload))
    _append_roll_mechanics(
        output,
        input_source=input_source,
        page=page,
        base_separation_dpi=model.base_separation_dpi,
    )
    if model.identity in {"SureColorP600", "SureColorP800", "Stylus_Photo_R3000"}:
        # R3000's Gutenprint ink lists specify IK 31/51; its captured photo
        # sheet confirms IK 31. P600/P800 cartridge captures confirm the pair.
        selected_ink = model.ink_configuration(ink_configuration)
        if selected_ink is None:
            raise ValueError(f"{model.identity} has no black ink configuration")
        ink_switch = {
            "PK": b"\x00\x31",
            "MK": b"\x00\x51",
        }.get(selected_ink.active_logical_ink)
        if ink_switch is None:
            raise ValueError(f"Unsupported {model.identity} black ink configuration")
        output.extend(commands.remote_payload("IK", ink_switch))
    output.extend(commands.remote_mode_exit())
    output.extend(commands.graphics_mode())
    native_mode = _profile_mode(mode, model)
    if profile.mode_derived_units:
        output.extend(
            commands.units(
                page=native_mode.page_management_units,
                vertical=native_mode.vertical_units,
                horizontal=native_mode.horizontal_units,
                scale=native_mode.unit_scale,
            )
        )
    else:
        units_payload = (
            _p700_mode_setup(mode)[0]
            if profile.identity == "P700RIP" else _et8550_mode_setup(mode)[0]
            if profile.identity == "ET8550" else profile.units_payload
        )
        output.extend(commands.extended_payload("U", units_payload))
    output.extend(commands.gray_mode(2))
    output.extend(commands.microweave(
        mode.escp2_printer_weave_mode or 0,
        variant=mode.escp2_printer_weave_variant,
    ))
    output.extend(commands.unidirectional(direction is PrintDirection.UNIDIRECTIONAL))
    if mode.escp2_emit_legacy_s_mode_2:
        # Some early Epson modes require exactly ``ESC ( s 01 00 02`` after
        # ESC U. The mode schema exposes this as a boolean instead of retaining
        # the source bit mask.
        output.extend(commands.extended_payload("s", bytes((2,))))
    dot_size = (
        mode.escp2_dot_size_override
        if mode.escp2_dot_size_override is not None
        else mode.dot_size
    )
    output.extend(commands.dot_size(dot_size))
    if profile.mode_derived_units:
        output.extend(
            commands.printhead_resolution(
                scale=native_mode.resolution_scale,
                vertical=native_mode.printhead_vertical_unit,
                horizontal=native_mode.printhead_horizontal_unit,
            )
        )
    elif profile.identity != "ET8550":
        head_payload = (
            _p700_mode_setup(mode)[1]
            if profile.identity == "P700RIP" else _et8550_mode_setup(mode)[1]
            if profile.identity == "ET8550" else profile.printhead_resolution_payload
        )
        output.extend(commands.extended_payload("D", head_payload))
    output.extend(commands.page_length(geometry.height_page_units))
    output.extend(
        commands.page_format(
            geometry.format_top_vertical_units,
            geometry.format_bottom_vertical_units,
        )
    )
    output.extend(
        commands.paper_dimensions(
            geometry.width_page_units,
            geometry.height_page_units,
        )
    )
    if profile.identity == "ET8550":
        # Reference streams set ESC (D after the page geometry.
        output.extend(commands.extended_payload("D", _et8550_mode_setup(mode)[1]))
    print_method = resolve_profile_print_method(
        model, mode, medium, device_settings
    )
    if print_method is not None:
        output.extend(commands.print_method(
            print_method, payload_length=mode.escp2_print_method_payload_length,
        ))
    prefix_end = len(output)
    for item in encoded_passes:
        output.extend(item)
    if not encoded_passes and not split_parts:
        output.extend(commands.line_feed())
    if not input_source.suppress_form_feed_after_page:
        output.extend(commands.form_feed())
    output.extend(commands.reset())
    output.extend(commands.remote_mode_enter())
    if model.catalog_package_id not in {"gp--escp2-r2880", "gp--escp2-r3000"}:
        for remote_command, payload in input_source.deinitialization_commands:
            output.extend(commands.remote_payload(remote_command, payload))
    output.extend(commands.remote_load_defaults())
    output.extend(commands.remote_job_end(0))
    output.extend(commands.remote_mode_exit())
    if split_parts:
        return bytes(output[:prefix_end]), bytes(output[prefix_end:])
    return bytes(output)


class EpsonEscp2Backend:
    """Render already routed physical planes; no QTR or logical-channel semantics."""

    family = "epson_escp2_model96"

    def render_stream(
        self,
        *,
        model: PrinterModel,
        mode: PrinterMode,
        medium: MediaCapability,
        page: PageCapability,
        input_source: InputSourceCapability,
        rasters: PhysicalRasterSet | StreamingPhysicalRasterSet,
        write_chunk: Callable[[bytes], None],
        cancel_check: Callable[[], None] | None = None,
        direction: PrintDirection = PrintDirection.BIDIRECTIONAL,
        job_label: str = "EPSONRIP",
        horizontal_offset_pixels: int = 0,
        vertical_offset_rows: int = 0,
        allow_unsupported_ppd_combinations: bool = False,
        device_settings: dict[str, str] | None = None,
        ink_configuration: str | None = None,
    ) -> BackendStreamResult:
        """Render and emit native passes without retaining the page's PRN.

        The caller owns transport and the failure policy.  This method never
        sends the normal page trailer when scheduling or encoding fails.
        """

        if model.backend_family != self.family and model.escp2_profile is None:
            raise ValueError(f"Unsupported ESC/P2 backend: {model.backend_family}")
        if not mode.implemented or not medium.implemented or not page.implemented or not input_source.implemented:
            raise ValueError("Selected printer mode, medium, page or input source is unavailable")
        if medium.supported_modes and mode.identity not in medium.supported_modes:
            raise ValueError("Selected medium does not support the printer mode")
        if model.media_page_constraints(medium.identity, page.ppd.name) and not allow_unsupported_ppd_combinations:
            raise ValueError("PPD forbids the selected medium and page combination")
        if rasters.bit_depth != mode.bit_depth:
            raise ValueError("Physical raster bit depth differs from printer mode")

        profile_path = model.escp2_profile is not None and model.backend_family != self.family
        if profile_path:
            page = model.native_page(page, mode, input_source)
            # Factory ink roles describe the original ink set. Explicit raster
            # routing also supports refilled channels, independently of paper.
            native_mode = _profile_mode(mode, model)
            top_offset, left_offset = _profile_imageable_origin_offsets(
                model, page, native_mode, input_source=input_source,
            )
            first_line = top_offset + vertical_offset_rows
            page_height_rows = round((page.ppd.height_points + 10) * mode.vertical_dpi / 72)
            left_units = (
                left_offset
                + horizontal_offset_pixels * native_mode.horizontal_units // mode.horizontal_dpi
            )
            geometry = _mode_page_geometry(
                page,
                _profile_page_units(model, native_mode),
                leading_feed_offset_mils=_profile_leading_feed_offset_mils(model),
                use_imageable_margins=model.escp2_command_set == "Pro",
                use_imageable_bottom=_profile_uses_imageable_bottom(
                    model, page, input_source
                ),
                bottom_margin_mils=_profile_bottom_margin_mils(model),
                top_margin_mils=_profile_top_margin_mils(model, page, input_source),
                borderless_bottom_extension_mils=(
                    _profile_borderless_bottom_extension_mils(model, input_source)
                ),
                borderless_top_extra_unit=(model.catalog_package_id != "gp--escp2-r2880"
                    and not (model.escp2_profile.identity in {"ET8550", "P700RIP"} and page.native_page_format_mils)),
            )
            parts = _encode_profile_stream(
                model=model,
                mode=mode,
                medium=medium,
                page=page,
                input_source=input_source,
                geometry=geometry,
                encoded_passes=(),
                job_label=job_label,
                direction=direction,
                device_settings=device_settings,
                ink_configuration=ink_configuration,
                split_parts=True,
            )
            classification = "IMPORTED_ESCP2_PROFILE"
            path_definitions = model.physical_paths
        else:
            legacy_contract = (
                mode.identity == "360x180sw"
                and medium.identity == "PLAIN_NORMAL"
                and input_source.identity == "LowerFront"
                and horizontal_offset_pixels == 0
                and vertical_offset_rows == 0
            )
            if legacy_contract:
                if isinstance(rasters, StreamingPhysicalRasterSet):
                    raise ValueError("verified 360x180 canvas needs retained physical rasters")
                padded, legacy_passes = schedule_xp750_360x180(rasters)
                profile = replace(
                    XP750_PHASE3C,
                    raster_width_pixels=padded.width_pixels,
                    pass_count=padded.height_rows // 180,
                    page=_page_geometry(page),
                )
                legacy_parts = _encode_fixed_model96_stream(
                    remote_setup_commands=_selected_device_commands(model, device_settings),
                    profile=profile,
                    color_configuration=2,
                    job_label=job_label,
                    encoded_passes=(),
                    direction=direction,
                    paper_path_type=input_source.path_type,
                    paper_path_number=input_source.path_number,
                    split_parts=True,
                )
                assert isinstance(legacy_parts, tuple)
                if cancel_check is not None:
                    cancel_check()
                write_chunk(legacy_parts[0])
                for legacy_pass in legacy_passes:
                    if cancel_check is not None:
                        cancel_check()
                    write_chunk(legacy_pass.with_compressed_rows().encode())
                if not legacy_passes:
                    write_chunk(commands.line_feed())
                write_chunk(legacy_parts[1])
                classification = (
                    "LEGACY_360X180_BORDERED"
                    if page.ppd.name == "Letter" else "MODEL96_BORDERED_FORMULA"
                )
                return BackendStreamResult(len(legacy_passes), classification)
            native_mode = XP750_MODEL96_MODE_BY_ID[mode.identity]
            first_line = native_mode.printed_vertical_dpi + vertical_offset_rows
            page_height_rows = native_mode.printed_vertical_dpi + round(
                (page.ppd.height_points + 10) * native_mode.printed_vertical_dpi / 72
            )
            left_units = horizontal_offset_pixels * native_mode.horizontal_units // native_mode.printed_horizontal_dpi
            geometry = _mode_page_geometry(page, native_mode.page_management_units)
            parts = _encode_mode96_stream(
                remote_setup_commands=_selected_device_commands(model, device_settings),
                mode_id=mode.identity,
                medium=medium,
                page=page,
                input_source=input_source,
                geometry=geometry,
                encoded_passes=(),
                job_label=job_label,
                direction=direction,
                split_parts=True,
            )
            classification = "MODEL96_BORDERED_FORMULA"
            path_definitions = None

        assert isinstance(parts, tuple)
        prefix, suffix = parts
        if cancel_check is not None:
            cancel_check()
        write_chunk(prefix)
        pass_count = 0

        def emit_pass(item: Any) -> None:
            nonlocal pass_count
            if cancel_check is not None:
                cancel_check()
            # PackedRasterPass validates row count and width at construction;
            # its Cython-produced PackBits rows are encoded only once here.
            # The optional strict-offline path performs full decode/validation
            # before opening a connection.
            write_chunk(item.encode())
            pass_count += 1

        schedule_model96_softweave(
            rasters,
            native_mode,
            first_line=first_line,
            page_height_rows=page_height_rows,
            left_position_units=left_units,
            initial_feed_offset_rows=_profile_initial_feed_offset_rows(
                model, native_mode, page=page, input_source=input_source,
            ),
            path_definitions=path_definitions,
            pass_sink=emit_pass,
            cancel_check=cancel_check,
        )
        if cancel_check is not None:
            cancel_check()
        if pass_count == 0:
            write_chunk(commands.line_feed())
        write_chunk(suffix)
        return BackendStreamResult(pass_count, classification)

    def render(
        self,
        *,
        model: PrinterModel,
        mode: PrinterMode,
        medium: MediaCapability,
        page: PageCapability,
        input_source: InputSourceCapability,
        rasters: PhysicalRasterSet | StreamingPhysicalRasterSet,
        direction: PrintDirection = PrintDirection.BIDIRECTIONAL,
        job_label: str = "EPSONRIP",
        horizontal_offset_pixels: int = 0,
        vertical_offset_rows: int = 0,
        allow_unsupported_ppd_combinations: bool = False,
        device_settings: dict[str, str] | None = None,
        ink_configuration: str | None = None,
        trace: PerformanceTrace | None = None,
    ) -> BackendRenderResult:
        if model.backend_family != self.family and model.escp2_profile is None:
            raise ValueError(f"Backend {self.family} не обслуживает {model.backend_family}")
        if not mode.implemented:
            raise ValueError(f"Mode {mode.identity} заблокирован: {mode.blocked_reason}")
        if not medium.implemented:
            raise ValueError(f"Media {medium.identity} заблокирован: {medium.blocked_reason}")
        if medium.supported_modes and mode.identity not in medium.supported_modes:
            raise ValueError(
                f"Media {medium.identity} не поддерживает mode {mode.identity}"
            )
        if not page.implemented:
            raise ValueError(f"Page {page.ppd.name} заблокирована: {page.blocked_reason}")
        if not input_source.implemented:
            raise ValueError(
                f"Input source {input_source.identity} заблокирован: {input_source.blocked_reason}"
            )
        rejected = model.media_page_constraints(medium.identity, page.ppd.name)
        if rejected and not allow_unsupported_ppd_combinations:
            raise ValueError(
                f"PPD запрещает сочетание MediaType={medium.identity}, PageSize={page.ppd.name}"
            )
        if model.escp2_profile is not None and model.backend_family != self.family:
            page = model.native_page(page, mode, input_source)
            # Paper selection does not restrict explicitly routed ink channels.
            native_mode = _profile_mode(mode, model)
            # Raster rows and placement offsets are relative to the imageable
            # top. Pro profiles place the top margin in ESC (c; cut-sheet
            # profiles need the PPD top offset in the raster coordinates.
            top_offset, left_offset = _profile_imageable_origin_offsets(
                model, page, native_mode, input_source=input_source,
            )
            first_line = top_offset + vertical_offset_rows
            page_height_rows = round(
                (page.ppd.height_points + 10) * mode.vertical_dpi / 72
            )
            left_units = (
                left_offset
                + horizontal_offset_pixels
                * native_mode.horizontal_units
                // mode.horizontal_dpi
            )
            timings = WeaveStageTimings() if trace is not None else None
            if trace is None:
                weave_schedule = schedule_model96_softweave(
                    rasters,
                    native_mode,
                    first_line=first_line,
                    page_height_rows=page_height_rows,
                    left_position_units=left_units,
                    initial_feed_offset_rows=_profile_initial_feed_offset_rows(
                        model, native_mode, page=page, input_source=input_source,
                    ),
                    path_definitions=model.physical_paths,
                )
            else:
                with trace.stage("weave scheduling"):
                    weave_schedule = schedule_model96_softweave(
                        rasters,
                        native_mode,
                        first_line=first_line,
                        page_height_rows=page_height_rows,
                        left_position_units=left_units,
                        initial_feed_offset_rows=_profile_initial_feed_offset_rows(
                            model, native_mode, page=page, input_source=input_source,
                        ),
                        path_definitions=model.physical_paths,
                        trace=trace,
                        timings=timings,
                    )
                assert timings is not None
                trace.record_duration(
                    "Cython unpack/split/fold",
                    timings.cython_unpack_split_fold,
                    measurement="aggregate",
                )
                trace.record_duration(
                    "Cython weave assignment",
                    timings.cython_assignment,
                    measurement="aggregate",
                )
                trace.record_duration(
                    "PackBits",
                    timings.packbits,
                    measurement="aggregate",
                )
            passes = tuple(
                item.with_compressed_rows(timings) for item in weave_schedule.passes
            )
            weave_schedule = replace(weave_schedule, passes=passes)
            geometry = _mode_page_geometry(
                page,
                _profile_page_units(model, native_mode),
                leading_feed_offset_mils=_profile_leading_feed_offset_mils(model),
                use_imageable_margins=model.escp2_command_set == "Pro",
                use_imageable_bottom=_profile_uses_imageable_bottom(
                    model, page, input_source
                ),
                bottom_margin_mils=_profile_bottom_margin_mils(model),
                top_margin_mils=_profile_top_margin_mils(model, page, input_source),
                borderless_bottom_extension_mils=(
                    _profile_borderless_bottom_extension_mils(model, input_source)
                ),
                borderless_top_extra_unit=(model.catalog_package_id != "gp--escp2-r2880"
                    and not (model.escp2_profile.identity in {"ET8550", "P700RIP"} and page.native_page_format_mils)),
            )
            encoded_passes = tuple(item.encode(timings) for item in passes)
            stream_started = perf_counter()
            data = _encode_profile_stream(
                model=model,
                mode=mode,
                medium=medium,
                page=page,
                input_source=input_source,
                geometry=geometry,
                encoded_passes=encoded_passes,
                job_label=job_label,
                direction=direction,
                device_settings=device_settings,
                ink_configuration=ink_configuration,
            )
            if trace is not None:
                assert timings is not None
                timings.escp2_raster_construction += perf_counter() - stream_started
                trace.record_duration(
                    "ESC/P2 construction",
                    timings.escp2_raster_construction,
                    measurement="aggregate",
                )
            return BackendRenderResult(
                job=GeneratedModeRasterJob(data=data, rasters=rasters, passes=passes),
                model=model,
                mode=mode,
                medium=medium,
                page=page,
                input_source=input_source,
                ppd_constraints_checked=model.ppd.constraints,
                page_formula_classification="IMPORTED_ESCP2_PROFILE",
                weave_schedule=weave_schedule,
            )
        legacy_contract = (
            mode.identity == "360x180sw"
            and medium.identity == "PLAIN_NORMAL"
            and input_source.identity == "LowerFront"
            and horizontal_offset_pixels == 0
            and vertical_offset_rows == 0
        )
        weave_schedule = None
        if legacy_contract:
            if isinstance(rasters, StreamingPhysicalRasterSet):
                raise ValueError("legacy 360x180 contract requires retained full rasters")
            if trace is None:
                padded, passes = schedule_xp750_360x180(rasters)
            else:
                with trace.stage("weave scheduling", scheduler="verified-360x180"):
                    padded, passes = schedule_xp750_360x180(rasters)
            geometry = _page_geometry(page)
            profile = replace(
                XP750_PHASE3C,
                raster_width_pixels=padded.width_pixels,
                pass_count=padded.height_rows // 180,
                page=geometry,
            )
            if trace is None:
                passes = tuple(item.with_compressed_rows() for item in passes)
                encoded_passes = tuple(item.encode() for item in passes)
                data = _encode_fixed_model96_stream(
                    remote_setup_commands=_selected_device_commands(model, device_settings),
                    profile=profile,
                    color_configuration=2,
                    job_label=job_label,
                    encoded_passes=encoded_passes,
                    blank_page=not encoded_passes,
                    direction=direction,
                    paper_path_type=input_source.path_type,
                    paper_path_number=input_source.path_number,
                )
            else:
                with trace.stage("PackBits"):
                    passes = tuple(item.with_compressed_rows() for item in passes)
                with trace.stage("ESC/P2 construction"):
                    encoded_passes = tuple(item.encode() for item in passes)
                    data = _encode_fixed_model96_stream(
                        remote_setup_commands=_selected_device_commands(model, device_settings),
                        profile=profile,
                        color_configuration=2,
                        job_label=job_label,
                        encoded_passes=encoded_passes,
                        blank_page=not encoded_passes,
                        direction=direction,
                        paper_path_type=input_source.path_type,
                        paper_path_number=input_source.path_number,
                    )
                trace.event(
                    "weave RESULT",
                    scheduled_passes=len(passes),
                    hpasses=mode.horizontal_passes,
                    vpasses=mode.vertical_passes,
                )
            job: GeneratedPhysicalRasterJob | GeneratedModeRasterJob = GeneratedPhysicalRasterJob(
                data=data, rasters=padded, passes=passes
            )
        else:
            escp2_mode = XP750_MODEL96_MODE_BY_ID[mode.identity]
            first_line = escp2_mode.printed_vertical_dpi + vertical_offset_rows
            page_height_rows = escp2_mode.printed_vertical_dpi + round(
                (page.ppd.height_points + 10) * escp2_mode.printed_vertical_dpi / 72
            )
            left_units = (
                horizontal_offset_pixels
                * escp2_mode.physical_horizontal_dpi
                // escp2_mode.printed_horizontal_dpi
            )
            timings = WeaveStageTimings() if trace is not None else None
            if trace is not None:
                trace.event("Cython unpack/split/fold START", measurement="aggregate")
                trace.event("Cython weave assignment START", measurement="aggregate")
            if trace is None:
                weave_schedule = schedule_model96_softweave(
                    rasters,
                    escp2_mode,
                    first_line=first_line,
                    page_height_rows=page_height_rows,
                    left_position_units=left_units,
                )
            else:
                with trace.stage("weave scheduling"):
                    weave_schedule = schedule_model96_softweave(
                        rasters,
                        escp2_mode,
                        first_line=first_line,
                        page_height_rows=page_height_rows,
                        left_position_units=left_units,
                        trace=trace,
                        timings=timings,
                    )
                assert timings is not None
                trace.record_duration(
                    "Cython unpack/split/fold",
                    timings.cython_unpack_split_fold,
                    measurement="aggregate",
                )
                trace.record_duration(
                    "Cython weave assignment",
                    timings.cython_assignment,
                    measurement="aggregate",
                )
            passes = weave_schedule.passes
            geometry = _mode_page_geometry(page, escp2_mode.page_management_units)
            if trace is None:
                passes = tuple(item.with_compressed_rows() for item in passes)
                weave_schedule = replace(weave_schedule, passes=passes)
                data = _encode_mode96_stream(
                    remote_setup_commands=_selected_device_commands(model, device_settings),
                    mode_id=mode.identity,
                    medium=medium,
                    page=page,
                    input_source=input_source,
                    geometry=geometry,
                    encoded_passes=tuple(item.encode() for item in passes),
                    job_label=job_label,
                    direction=direction,
                )
            else:
                assert timings is not None
                trace.event("PackBits START", measurement="aggregate")
                trace.event("ESC/P2 construction START", measurement="aggregate")
                passes = tuple(item.with_compressed_rows(timings) for item in passes)
                weave_schedule = replace(weave_schedule, passes=passes)
                encoded_passes = tuple(item.encode(timings) for item in passes)
                stream_started = perf_counter()
                data = _encode_mode96_stream(
                    remote_setup_commands=_selected_device_commands(model, device_settings),
                    mode_id=mode.identity,
                    medium=medium,
                    page=page,
                    input_source=input_source,
                    geometry=geometry,
                    encoded_passes=encoded_passes,
                    job_label=job_label,
                    direction=direction,
                )
                timings.escp2_raster_construction += perf_counter() - stream_started
                trace.record_duration("PackBits", timings.packbits, measurement="aggregate")
                trace.record_duration(
                    "ESC/P2 construction",
                    timings.escp2_raster_construction,
                    measurement="aggregate",
                )
            job = GeneratedModeRasterJob(data=data, rasters=rasters, passes=passes)
        return BackendRenderResult(
            job=job,
            model=model,
            mode=mode,
            medium=medium,
            page=page,
            input_source=input_source,
            ppd_constraints_checked=model.ppd.constraints,
            page_formula_classification=(
                "LEGACY_360X180_BORDERED"
                if legacy_contract and page.ppd.name == "Letter"
                else "MODEL96_BORDERED_FORMULA"
            ),
            weave_schedule=weave_schedule,
        )
