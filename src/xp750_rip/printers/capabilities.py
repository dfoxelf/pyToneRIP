# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Generic printer capability and instance models."""

from __future__ import annotations

from dataclasses import dataclass, replace

from xp750_rip.capabilities.ppd import PpdCapabilities, PpdConstraint, PpdPage
from xp750_rip.raster import PhysicalPathId


@dataclass(frozen=True, slots=True)
class PhysicalPathDefinition:
    identity: PhysicalPathId
    factory_role: str | None = None
    # Native ESC i selector and head geometry belong to the printer descriptor,
    # never to generic routing. Селектор и геометрия головки являются данными
    # конкретной модели, а не свойствами generic PhysicalPath.
    escp2_selector: int | None = None
    head_offset: int = 0
    allocation_index: int = 0
    # Factory ink/media association for defaults, not a restriction on explicit
    # routing: refilled printers can use different inks in every physical path.
    media_classes: tuple[str, ...] = ()
    # Stable user-facing ink code. The logical QTR channel remains independent
    # from this factory label: for example logical K may be routed to physical
    # LK. / Стабильное обозначение физической позиции для GUI (PK, MK, LK,
    # LLK, C, LC...). Оно не заменяет пользовательскую QTR-маршрутизацию.
    short_name: str | None = None
    display_name: str | None = None
    # Some printers expose one ESC i address through alternative installed ink
    # configurations. P600/P800 use the same black raster selector for PK and
    # MK, so the selected configuration changes the physical label, not the
    # byte sent in every raster band.
    configuration_roles: tuple[tuple[str, str], ...] = ()
    # A few Epson office heads expose one physical ink through several native
    # ESC i banks (for example black at 0x41/0x42/0x44).  They are not three
    # user-selectable inks.  Keep one routable/QTR path while retaining every
    # address the backend must drive.
    additional_escp2_selectors: tuple[int, ...] = ()
    # Optional QTR curve used only to build the initial routing UI.  This is
    # deliberately separate from short_name, which identifies the physical
    # cartridge/nozzle bank shown on the right side of the mapping row.
    default_logical_ink: str | None = None

    def __post_init__(self) -> None:
        if self.escp2_selector is not None and not 0 <= self.escp2_selector <= 0xFF:
            raise ValueError("ESC/P2 selector must fit one byte")
        if any(not 0 <= value <= 0xFF for value in self.additional_escp2_selectors):
            raise ValueError("ESC/P2 selector must fit one byte")
        selectors = self.escp2_selectors
        if len(selectors) != len(set(selectors)):
            raise ValueError("ESC/P2 selectors for one physical path must be unique")

    @property
    def escp2_selectors(self) -> tuple[int, ...]:
        if self.escp2_selector is None:
            return self.additional_escp2_selectors
        return (self.escp2_selector, *self.additional_escp2_selectors)


@dataclass(frozen=True, slots=True)
class InkConfigurationCapability:
    """Selectable set of physically installed inks for one printer model.

    Конфигурация описывает установленный набор, например Photo Black или
    Matte Black. Это не цветовой профиль и не подмена QTR-кривых.
    """

    identity: str
    display_name: str
    active_logical_ink: str | None = None


@dataclass(frozen=True, slots=True)
class DeviceSettingChoice:
    identity: str
    display_name: str
    remote_command: str
    remote_payload: bytes
    # A small number of model options select an alternative value already
    # carried by the active print mode; they do not emit a REMOTE1 command.
    # Некоторые настройки выбирают второй байт самого режима и не являются
    # отдельной REMOTE1-командой.
    use_alternate_print_method: bool = False


@dataclass(frozen=True, slots=True)
class DeviceSettingCapability:
    identity: str
    display_name: str
    default_choice: str
    choices: tuple[DeviceSettingChoice, ...]

    def choice(self, identity: str) -> DeviceSettingChoice:
        for item in self.choices:
            if item.identity == identity:
                return item
        raise KeyError(identity)


@dataclass(frozen=True, slots=True)
class MaintenanceActionCapability:
    """One printer maintenance operation that the current backend can emit.

    Maintenance is intentionally separate from persistent print settings:
    these commands execute immediately and must never be inserted into an
    ordinary photograph job. / Обслуживание отделено от сохраняемых настроек:
    команда выполняется сразу и не должна попадать в обычное задание печати.
    """

    identity: str
    display_name: str
    description: str
    requires_paper: bool = False
    consumes_ink: bool = False


EPSON_REMOTE_MAINTENANCE_ACTIONS = (
    MaintenanceActionCapability(
        identity="head_clean",
        display_name="Clean print head",
        description="Run one standard print-head cleaning cycle.",
        consumes_ink=True,
    ),
    MaintenanceActionCapability(
        identity="nozzle_check",
        display_name="Print nozzle check",
        description="Ask the printer firmware to print its native nozzle pattern.",
        requires_paper=True,
    ),
    MaintenanceActionCapability(
        identity="test_page",
        display_name="Print pyToneRIP test page",
        description="Print a small raw per-channel pattern using the selected model settings.",
        requires_paper=True,
    ),
)


@dataclass(frozen=True, slots=True)
class EpsonEscp2BackendProfile:
    """Source-derived ESC/P2 framing shared by an Epson printer family.

    Raw payloads are stored because Epson families do not all follow the
    model-96 arithmetic. Сырые payload сохраняют найденные значения без
    обратного угадывания их семантики.
    """

    identity: str
    units_payload: bytes
    printhead_resolution_payload: bytes
    rows_per_pass: int
    default_dot_size: int
    default_print_method: int | None
    remote_extras: tuple[tuple[str, bytes], ...] = ()
    source: str = ""
    mode_derived_units: bool = False

    def __post_init__(self) -> None:
        if len(self.units_payload) != 5:
            raise ValueError("ESC (U profile payload must contain five bytes")
        if len(self.printhead_resolution_payload) != 4:
            raise ValueError("ESC (D profile payload must contain four bytes")
        if not 1 <= self.rows_per_pass <= 0xFFFF:
            raise ValueError("rows_per_pass must fit ESC i")
        if not 0 <= self.default_dot_size <= 0xFF:
            raise ValueError("default_dot_size must fit one byte")


@dataclass(frozen=True, slots=True)
class PrinterMode:
    identity: str
    display_name: str
    horizontal_dpi: int
    vertical_dpi: int
    physical_horizontal_dpi: int
    physical_vertical_dpi: int
    base_resolution_dpi: int
    horizontal_passes: int
    vertical_passes: int
    nozzle_count: int
    nozzle_separation: int
    bit_depth: int
    dot_size: int
    drop_sizes: tuple[float, ...]
    density: float
    source: str
    implemented: bool
    offline_validated: bool
    blocked_reason: str | None = None
    ordered_matrix_id: str = "matrix-2x1"
    # Source Mode.Param values override the family defaults when the
    # source provides per-resolution ESC (e / ESC (m values.
    escp2_dot_size_override: int | None = None
    escp2_print_method_override: int | None = None
    escp2_printer_weave_mode: int | None = None
    escp2_alternate_print_method: int | None = None
    escp2_default_direction: str = "bidirectional"
    escp2_emit_legacy_s_mode_2: bool = False
    escp2_internal_layout_flags: int = 0
    escp2_weave_pass_group_count: int = 1
    escp2_weave_variant_code: int = 0
    escp2_explicit_weave_sequence: tuple[tuple[int, int, int], ...] = ()
    media_class: str = "generic"
    # Advertised output quality can exceed the native command coordinate grid.
    # For example, ReferenceDriver ET-2550 PhotoPlus is 5760 x 1440 DPI while its
    # PrinterDPI grid remains 1440 x 1440. Keep the two values independent.
    output_resolution_dpi: tuple[int, int] | None = None

    # Explicit protocol dialects, never selected from DPI or family-name
    # guesses. Existing modes retain their captured one-byte commands.
    escp2_print_method_payload_length: int = 1
    escp2_printer_weave_variant: int | None = None

    def __post_init__(self) -> None:
        if self.escp2_print_method_payload_length not in (1, 4):
            raise ValueError("ESC (m payload length must be 1 or 4")
        method_limit = 0xFF if self.escp2_print_method_payload_length == 1 else 0xFFFF
        for method in (self.escp2_print_method_override, self.escp2_alternate_print_method):
            if method is not None and not 0 <= method <= method_limit:
                raise ValueError("Print method does not fit the selected ESC (m dialect")
        if self.escp2_printer_weave_variant is not None:
            if self.escp2_printer_weave_mode is None:
                raise ValueError("Extended firmware weave requires an explicit mode")
            if not 0 <= self.escp2_printer_weave_variant <= 0xFF:
                raise ValueError("Firmware weave variant must fit one byte")

    @property
    def escp2_ink_type(self) -> int:
        """Gutenprint model value sent by ESC (e for the drop-size mode."""

        return self.dot_size

@dataclass(frozen=True, slots=True)
class PageCapability:
    ppd: PpdPage
    implemented: bool
    offline_validated: bool
    blocked_reason: str | None = None
    media_size_code: int | None = None
    sheet_geometry_class: str = "standard_sheet"
    special_media_selector: int | None = None
    # Source ReferenceDriver sheet dimensions and trailing printable margin (mils).
    # Present only for page formats whose native ESC (c was checked against tpprint.
    native_page_format_mils: tuple[int, int, int] | None = None
    # Some ReferenceDriver borderless PPDs describe an enlarged raster canvas.
    # Windows PageMediaSize describes the physical sheet, not that canvas.
    physical_media_size_mm: tuple[float, float] | None = None

    @property
    def media_width_mm(self) -> float:
        return self.physical_media_size_mm[0] if self.physical_media_size_mm else self.ppd.width_mm

    @property
    def media_height_mm(self) -> float:
        return self.physical_media_size_mm[1] if self.physical_media_size_mm else self.ppd.height_mm


@dataclass(frozen=True, slots=True)
class MediaCapability:
    identity: str
    display_name: str
    source: str
    implemented: bool
    gutenprint_name: str | None = None
    blocked_reason: str | None = None
    media_code: int | None = None
    legacy_media_type_code: int | None = None
    special_media_selector: int | None = None
    # A model-local selector for physical head/raster layout. It is not an ICC
    # or RGB conversion value. / Селектор физической раскладки головки/растра
    # конкретной модели; к цветовому преобразованию отношения не имеет.
    physical_raster_layout_variant: int = 0
    print_methods: tuple[tuple[str, int], ...] = ()
    supported_modes: tuple[str, ...] = ()
    media_class: str = "generic"
    # Per-medium mechanical defaults copied from the open ESC/P2 media table.
    # These are native one-byte protocol values, not RGB/ICC corrections.
    # Механические значения относятся к выбранной бумаге и передаются как
    # REMOTE1-команды; они не изменяют QUAD-кривые или уровни каналов.
    paper_thickness_code: int | None = None
    vacuum_intensity_code: int | None = None
    feed_adjustment_code: int | None = None
    feed_sequence_code: int | None = None
    platen_gap_code: int | None = None

    def print_method(self, mode_id: str) -> int:
        for identity, value in self.print_methods:
            if identity == mode_id:
                return value
        raise KeyError(mode_id)


@dataclass(frozen=True, slots=True)
class InputSourceCapability:
    identity: str
    display_name: str
    source: str
    implemented: bool
    blocked_reason: str | None = None
    path_type: int | None = None
    path_number: int | None = None
    medium_kind: str = "automatic_cut_sheet"
    roll_cut_mode: str = "none"
    cutter_edge_trim_mode: str | None = None
    suppress_form_feed_after_page: bool = False
    initialization_commands: tuple[tuple[str, bytes], ...] = ()
    deinitialization_commands: tuple[tuple[str, bytes], ...] = ()


@dataclass(frozen=True, slots=True)
class PrinterModel:
    identity: str
    display_name: str
    backend_family: str
    physical_paths: tuple[PhysicalPathDefinition, ...]
    modes: tuple[PrinterMode, ...]
    media: tuple[MediaCapability, ...]
    pages: tuple[PageCapability, ...]
    input_sources: tuple[InputSourceCapability, ...]
    ppd: PpdCapabilities
    source_summary: tuple[str, ...]
    # Hidden compatibility aliases allow configurations saved by older builds
    # to keep working after Draft/Standard/Best duplicates disappear from UI.
    media_aliases: tuple[tuple[str, str], ...] = ()
    # Native-backend exceptions to legacy ESC/P-R media/page restrictions.
    # Raw PPD data remains intact; GUI and backend share this effective policy.
    native_media_page_overrides: tuple[tuple[str, str], ...] = ()
    device_settings: tuple[DeviceSettingCapability, ...] = ()
    ink_configurations: tuple[InkConfigurationCapability, ...] = ()
    default_ink_configuration: str | None = None
    escp2_profile: EpsonEscp2BackendProfile | None = None
    catalog_package_id: str | None = None
    command_level: int | None = None
    mechanical_feature_set: int | None = None
    leading_feed_offset_mils: int = 0
    # Apply PPD imageable-area offsets when the native raster origin is the
    # physical page edge. Opt in only after a model's printed position is known.
    raster_origin_at_page_edge: bool = False
    # Basic vertical row separation from the model mechanics. REMOTE1 CO uses
    # it to express a roll-cut position independently of raster resolution.
    # Базовый шаг строк модели; в этих единицах кодируется положение реза CO.
    base_separation_dpi: int = 360
    escp2_command_set: str | None = None
    escp2_unit_scale: int = 5760
    # interleave, medium kind, left/right/top/bottom in points.
    escp2_page_margins: tuple[tuple[str, str, tuple[float, ...]], ...] = ()

    def native_page(self, page: PageCapability, mode: PrinterMode,
                    feed: InputSourceCapability) -> PageCapability:
        """Use declared Pro margins for both raster placement and ESC (c.

        Ordinary PPD pages can describe the entire sheet. Pro models require
        the physical imageable rectangle for the selected weave and feed.
        """
        if (self.escp2_profile is not None and self.escp2_profile.identity == "P700RIP"
                and page.ppd.borderless and feed.medium_kind == "roll"):
            # Sheet captures use physical dimensions; continuous borderless
            # captures keep the descriptor's extra 200/300 mil canvas.
            original = self.page(page.ppd.name).native_page_format_mils
            if original is not None:
                return replace(page, native_page_format_mils=(
                    original[0] + 200, original[1] + 300, 0))
        if self.escp2_profile is not None and self.escp2_profile.identity == "ET8550":
            if feed.medium_kind == "roll":
                # Captured continuous jobs include one inch of leading sheet
                # travel. Always derive from the original page: native_page is
                # used by both the application and the backend.
                original = self.page(page.ppd.name).native_page_format_mils
                if original is not None:
                    page = replace(page, native_page_format_mils=(
                        original[0], original[1] + 1000, original[2]))
            rear_slot = feed.path_type == 2 and feed.path_number == 0
            if page.sheet_geometry_class == "long_sheet" and not rear_slot:
                raise ValueError("Long sheets require the rear paper feed slot.")
            if rear_slot and page.special_media_selector is None:
                # Epson documents a 20 mm unprintable trailing edge for this
                # straight physical path. Apply it before both normal and
                # calibration layout; the other physical feeds keep their PPD.
                if page.ppd.borderless:
                    raise ValueError("The rear paper feed slot requires a 20 mm bottom margin; select a page with borders.")
                adjusted = replace(page.ppd, imageable_bottom_points=max(
                    page.ppd.imageable_bottom_points, 20.0 * 72.0 / 25.4))
                if adjusted.printable_height_points <= 0:
                    raise ValueError(f"Rear slot margin exceeds page {adjusted.name}")
                return replace(page, ppd=adjusted)
        if self.escp2_command_set != "Pro" or page.special_media_selector is not None:
            return page
        interleave = "printer" if mode.escp2_printer_weave_mode is not None else "soft"
        media = "roll" if feed.medium_kind == "roll" else "sheet"
        for entry_interleave, entry_media, margins in self.escp2_page_margins:
            if (entry_interleave, entry_media) != (interleave, media):
                continue
            left, right, top, bottom = margins
            ppd = page.ppd
            adjusted = replace(
                ppd,
                imageable_left_points=max(ppd.imageable_left_points, left),
                imageable_right_points=min(ppd.imageable_right_points, ppd.width_points - right),
                imageable_top_points=min(ppd.imageable_top_points, ppd.height_points - top),
                imageable_bottom_points=max(ppd.imageable_bottom_points, bottom),
            )
            if adjusted.printable_width_points <= 0 or adjusted.printable_height_points <= 0:
                raise ValueError(f"Native margins exceed page {ppd.name}")
            return replace(page, ppd=adjusted)
        return page

    def media_page_constraints(self, media_id: str, page_id: str) -> tuple[PpdConstraint, ...]:
        medium = self.medium(media_id)
        if (medium.identity, page_id) in self.native_media_page_overrides:
            return ()
        # Imported direct-channel media names are native RIP capabilities and
        # do not always have a one-to-one choice in a vendor RGB-driver PPD.
        # Such a PPD cannot validate that native medium; applying a different
        # driver's choice by label would invent a restriction. Built-in models
        # and exact imported matches still use their PPD constraints normally.
        #
        # Импортированное имя носителя может отсутствовать в RGB PPD. В этом
        # случае PPD не является источником ограничения для native-печати.
        try:
            media_option = self.ppd.option("MediaType")
        except KeyError:
            return ()
        if medium.identity not in {item.name for item in media_option.choices}:
            return ()
        # Some imported Gutenprint packages were created with a deliberately
        # minimal fallback PPD.  Their native page list is authoritative and
        # can contain large-format sizes absent from that UI-only PPD.  It
        # cannot impose constraints on a page choice it does not describe.
        try:
            page_option = self.ppd.option("PageSize")
        except KeyError:
            return ()
        if page_id not in {item.name for item in page_option.choices}:
            return ()
        rejected = self.ppd.validate_choices(
            {"MediaType": medium.identity, "PageSize": page_id}
        )
        return rejected

    def device_setting(self, identity: str) -> DeviceSettingCapability:
        for item in self.device_settings:
            if item.identity == identity:
                return item
        raise KeyError(identity)

    @property
    def maintenance_actions(self) -> tuple[MaintenanceActionCapability, ...]:
        """Actions supported by a direct-channel Epson REMOTE1 backend.

        Gutenprint's Epson maintenance utility uses the same CH/VI/NC REMOTE1
        sequence for its ESC/P2 printer family. Models without a direct ESC/P2
        profile get no actions and therefore no Maintenance tab in the GUI.
        """

        if not self.backend_family.startswith("epson_escp2_"):
            return ()
        if self.escp2_profile is not None or self.backend_family == "epson_escp2_model96":
            return EPSON_REMOTE_MAINTENANCE_ACTIONS
        # A generic REMOTE1 maintenance sequence is still available when an
        # older locally imported definition lacks a renderable raster profile.
        # Do not advertise our generated test page until its backend is complete.
        return EPSON_REMOTE_MAINTENANCE_ACTIONS[:2]

    def maintenance_action(self, identity: str) -> MaintenanceActionCapability:
        for item in self.maintenance_actions:
            if item.identity == identity:
                return item
        raise KeyError(identity)

    def ink_configuration(self, identity: str | None) -> InkConfigurationCapability | None:
        if not self.ink_configurations:
            if identity is not None:
                raise KeyError(identity)
            return None
        selected = identity or self.default_ink_configuration
        for item in self.ink_configurations:
            if item.identity == selected:
                return item
        raise KeyError(selected)

    def active_logical_ink_names(
        self, selected: tuple[str, ...], configuration: str | None
    ) -> tuple[str, ...]:
        """Drop inactive alternatives before QTR evaluation and routing."""

        ink_configuration = self.ink_configuration(configuration)
        alternatives = {
            item.active_logical_ink
            for item in self.ink_configurations
            if item.active_logical_ink
        }
        if ink_configuration is None or not alternatives:
            return selected
        active = ink_configuration.active_logical_ink
        return tuple(item for item in selected if item not in alternatives or item == active)

    def __post_init__(self) -> None:
        path_ids = tuple(item.identity for item in self.physical_paths)
        if not path_ids or len(path_ids) != len(set(path_ids)):
            raise ValueError("PrinterModel должен объявлять уникальные PhysicalPaths")
        configuration_ids = tuple(item.identity for item in self.ink_configurations)
        if len(configuration_ids) != len(set(configuration_ids)):
            raise ValueError("PrinterModel must declare unique ink configurations")
        if self.default_ink_configuration is not None and (
            self.default_ink_configuration not in configuration_ids
        ):
            raise ValueError("Default ink configuration is absent from PrinterModel")

    def mode(self, identity: str) -> PrinterMode:
        for item in self.modes:
            if item.identity == identity:
                return item
        raise KeyError(identity)

    def medium(self, identity: str) -> MediaCapability:
        canonical = dict(self.media_aliases).get(identity, identity)
        for item in self.media:
            if item.identity == canonical:
                return item
        raise KeyError(identity)

    def page(self, identity: str) -> PageCapability:
        for item in self.pages:
            if item.ppd.name == identity:
                return item
        raise KeyError(identity)

    def input_source(self, identity: str) -> InputSourceCapability:
        for item in self.input_sources:
            if item.identity == identity:
                return item
        raise KeyError(identity)

    def selectable_input_sources(self) -> tuple[InputSourceCapability, ...]:
        """Put a physical feed compatible with the default page first."""
        visible = self.input_sources
        if not visible:
            raise ValueError(f"{self.identity} has no physical input source")

        # XP-830 excludes A4 from Cassette 1, so its default must use Cassette 2.
        try:
            choices = self.ppd.option("InputSlot").choices
            page = self.ppd.option("PageSize").default
        except KeyError:
            choices = ()
            page = None
        if page and choices:
            def key(value: str) -> str:
                return "".join(char for char in value.casefold() if char.isalnum())

            def choice_name(feed: InputSourceCapability) -> str | None:
                names = {key(feed.identity), key(feed.display_name)}
                return next((
                    choice.name for choice in choices
                    if key(choice.name) in names or key(choice.label) in names
                ), None)

            def allowed(feed: InputSourceCapability) -> bool:
                choice = choice_name(feed)
                if choice is None:
                    return True
                selections = {
                    "InputSlot": choice, "PageSize": page, "PageRegion": page,
                }
                return not any(rule.rejects(selections) for rule in self.ppd.constraints)

            if not visible[0].implemented or not allowed(visible[0]):
                selected = next((
                    feed for feed in visible[1:]
                    if feed.implemented and choice_name(feed) is not None
                    and allowed(feed) and feed.medium_kind != "roll"
                ), None)
                if selected is not None:
                    visible = (selected, *(feed for feed in visible if feed is not selected))
        return visible


@dataclass(frozen=True, slots=True)
class PrinterInstance:
    model: PrinterModel
    name: str
    transport_type: str | None = None
    host: str | None = None
    port: int = 9100


class PrinterRegistry:
    def __init__(
        self, models: tuple[PrinterModel, ...], *, aliases: dict[str, str] | None = None
    ) -> None:
        self._models = {model.identity.casefold(): model for model in models}
        if len(self._models) != len(models):
            raise ValueError("PrinterRegistry содержит повторяющийся model ID")
        self._aliases = {
            alias.casefold(): canonical.casefold()
            for alias, canonical in (aliases or {}).items()
        }
        if any(alias in self._models or canonical not in self._models
               for alias, canonical in self._aliases.items()):
            raise ValueError("PrinterRegistry содержит неверный alias model ID")

    @property
    def models(self) -> tuple[PrinterModel, ...]:
        return tuple(self._models.values())

    def get(self, identity: str) -> PrinterModel:
        try:
            key = identity.casefold()
            return self._models[self._aliases.get(key, key)]
        except KeyError as error:
            raise KeyError(f"Неизвестная модель принтера: {identity}") from error
