# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Product CLI adapter over reusable RIP application services."""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from time import perf_counter
from typing import Any

from xp750_rip.analysis.escp2_parser import format_stream, parse_stream
from xp750_rip.application import (
    CalibrationConfiguration,
    HalftoneConfiguration,
    LiveRenderSummary,
    MonoRipService,
    OutputConfiguration,
    PrintJobConfiguration,
)
from xp750_rip.config import UserConfig, load_user_config
from xp750_rip.contone import LogicalChannelId
from xp750_rip.direction import PrintDirection, parse_print_direction
from xp750_rip.dither.strategy import HalftoneAlgorithm
from xp750_rip.layout import ImageSizeRequest, LayoutOrientation, ResamplingMethod
from xp750_rip.performance import PerformanceTrace
from xp750_rip.printers.capabilities import PrinterInstance, PrinterModel, PrinterRegistry
from xp750_rip.printers.importer import (
    DEFINITION_DIR,
    GUTENPRINT_ROOT,
    import_report,
    normalize_import,
    persist_definition,
)
from xp750_rip.printers.xp750_capabilities import (
    default_registry,
    load_xp750_model,
    load_xp960_model,
)
from xp750_rip.qtr.bundle import QtrProfileBundle, load_profile_bundle
from xp750_rip.qtr.runtime_controls import (
    QtrAdvancedAdjustments,
    QtrBlendConfiguration,
    QtrRuntimeProfileSet,
    QtrToneBlendWeights,
    trace_runtime_code,
)
from xp750_rip.raster import PhysicalPathId
from xp750_rip.routing import ChannelRouter
from xp750_rip.transport import DEFAULT_RAW_TCP_TIMEOUT_SECONDS, RawTcpTransport, StreamingTransport


def _add_printer_selector(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--printer", help="ID модели принтера (по умолчанию XP750)")


def _add_profile_arguments(parser: argparse.ArgumentParser, *, positional: bool = False) -> None:
    if positional:
        parser.add_argument("quad", nargs="?", type=Path, help="готовый compiled QTR .quad")
        parser.add_argument("--quad", dest="quad_option", type=Path, help="готовый compiled QTR .quad")
    else:
        parser.add_argument("--quad", type=Path, help="готовый compiled QTR .quad")


def _add_job_arguments(parser: argparse.ArgumentParser, *, allow_send: bool) -> None:
    parser.add_argument("image", type=Path, help="входной grayscale TIFF")
    _add_profile_arguments(parser)
    parser.add_argument("--curve1", type=Path, help="Curve 1; --quad является shorthand")
    parser.add_argument("--curve2", type=Path, help="optional Curve 2 compiled .quad")
    parser.add_argument("--curve3", type=Path, help="optional Curve 3 compiled .quad")
    parser.add_argument(
        "--blend",
        help="global active-profile weights через запятую, сумма ровно 100",
    )
    parser.add_argument(
        "--split-tone",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="включить независимые H/M/S anchors",
    )
    parser.add_argument("--blend-highlights", help="H weights через запятую")
    parser.add_argument("--blend-midtones", help="M weights через запятую")
    parser.add_argument("--blend-shadows", help="S weights через запятую")
    parser.add_argument("--gamma", type=int, help="EpsonRIP gamma adjustment -15..15")
    parser.add_argument("--ink-limit", type=int, help="EpsonRIP ink-limit scale -50..50")
    parser.add_argument("--calibration-mode", choices=("rgb_selectors", "per_channel"),
                        help="raw calibration target mode; bypasses QUAD curves and blending")
    parser.add_argument("--calibration-ink-limit", type=int, help="raw calibration ink limit, 0..100 percent")
    parser.add_argument(
        "--direction", choices=[item.value for item in PrintDirection], default=None,
        help="направление печати: bidirectional или unidirectional",
    )
    parser.add_argument(
        "--dither",
        choices=[item.value for item in HalftoneAlgorithm],
        help="halftone strategy",
    )
    parser.add_argument("--config", type=Path, help="TOML-конфигурация job/preset")
    parser.add_argument("--mapping", type=Path, help="отдельный TOML с [routing]/[inkset]")
    _add_printer_selector(parser)
    parser.add_argument("--printer-name", help="имя экземпляра принтера")
    parser.add_argument("--mode", help="PrinterMode ID")
    parser.add_argument("--media", help="MediaType ID")
    parser.add_argument("--page", help="PageSize ID")
    parser.add_argument(
        "--allow-unsupported-ppd-combinations",
        action="store_true",
        help="expert override: bypass PPD MediaType/PageSize constraints",
    )
    parser.add_argument("--input-source", help="источник бумаги")
    parser.add_argument(
        "--ink-configuration",
        help="installed interchangeable ink configuration, for example PK or MK",
    )
    parser.add_argument(
        "--device-setting",
        action="append",
        default=[],
        metavar="SETTING=CHOICE",
        help="model-specific Epson device setting; parameter may be repeated",
    )
    parser.add_argument("--inks", help="logical inks: all либо список через запятую")
    parser.add_argument(
        "--map",
        action="append",
        default=[],
        metavar="LOGICAL=PATH",
        help="явный route; параметр можно повторять",
    )
    size = parser.add_mutually_exclusive_group()
    size.add_argument("--scale-percent", type=float, help="масштаб от TIFF native size")
    size.add_argument("--fit", action="store_true", default=None, help="вписать в printable region")
    size.add_argument(
        "--native-size",
        action="store_true",
        default=None,
        help="размер по TIFF resolution metadata",
    )
    parser.add_argument("--width-mm", type=float, help="физическая ширина с сохранением пропорций")
    parser.add_argument("--height-mm", type=float, help="физическая высота с сохранением пропорций")
    parser.add_argument(
        "--orientation",
        choices=[item.value for item in LayoutOrientation],
        help="ориентация layout",
    )
    parser.add_argument(
        "--windows-page",
        action="store_true",
        help="TIFF является уже скомпонованным полным листом Windows",
    )
    parser.add_argument(
        "--resampling",
        choices=[item.value for item in ResamplingMethod],
        help="метод изменения размера",
    )
    parser.add_argument("--center", action="store_true", default=None, help="центрировать изображение")
    parser.add_argument("--x-mm", type=float, help="смещение от левого края printable region")
    parser.add_argument("--y-mm", type=float, help="смещение от верхнего края printable region")
    parser.add_argument("--output", type=Path, help="выходной native .prn")
    parser.add_argument("--manifest", type=Path, help="выходной JSON manifest")
    parser.add_argument(
        "--diagnostics-dir",
        type=Path,
        help="каталог тяжёлых development diagnostics (не создаются по умолчанию)",
    )
    parser.add_argument(
        "--performance-log",
        type=Path,
        help="UTF-8 timing log крупных стадий production pipeline",
    )
    if allow_send:
        parser.add_argument(
            "--send",
            action="store_true",
            help="явно разрешить передачу уже validated bytes",
        )
        parser.add_argument("--host", help="адрес raw TCP printer instance")
        parser.add_argument("--port", type=int, help="raw TCP port (по умолчанию 9100)")
        parser.add_argument("--timeout", type=float, help="тайм-аут raw TCP в секундах")


def add_product_commands(subparsers: Any) -> None:
    command = subparsers.add_parser(
        "psa-export-capabilities",
        help="экспортировать read-only registry/capabilities для Windows PSA",
    )
    command.add_argument("--output", required=True, type=Path, help="выходной JSON")
    command.set_defaults(phase8_handler=_psa_export_capabilities)
    command = subparsers.add_parser(
        "psa-export-pdc",
        help="экспортировать Windows PrintDeviceCapabilities из backend registry",
    )
    command.add_argument("--output", required=True, type=Path, help="выходной PDC XML")
    command.add_argument(
        "--model-output-dir",
        type=Path,
        help="каталог отдельных PDC для каждой модели",
    )
    command.add_argument(
        "--fallback-only",
        action="store_true",
        help="записать в основной PDC только безопасный A4 fallback",
    )
    command.set_defaults(phase8_handler=_psa_export_pdc)
    command = subparsers.add_parser(
        "psa-generate-package-assets",
        help="сгенерировать Windows package assets из PrinterRegistry",
    )
    command.add_argument("--output-dir", required=True, type=Path)
    command.add_argument("--version", required=True)
    command.set_defaults(phase8_handler=_psa_generate_package_assets)
    command = subparsers.add_parser(
        "psa-bridge-job",
        help="проверить captured PSA job record и вернуть безопасные render argv tokens",
    )
    command.add_argument("record", type=Path, help="путь к job-record.json")
    command.add_argument("--output", type=Path, help="необязательный bridge-result JSON")
    command.set_defaults(phase8_handler=_psa_bridge_job)
    command = subparsers.add_parser(
        "psa-render-job",
        help="создать PRN из validated PSA job и подготовленного grayscale TIFF без отправки",
    )
    command.add_argument("record", type=Path, help="путь к job-record.json")
    command.add_argument("--input-tiff", type=Path, help="результат Windows PDL adapter")
    command.add_argument(
        "--curve-root",
        type=Path,
        help="каталог локальных копий captured .quad profiles для безопасного replay",
    )
    command.add_argument(
        "--normalized-tiff",
        type=Path,
        help="строгий grayscale TIFF; по умолчанию рядом с job record",
    )
    command.add_argument("--output-prn", required=True, type=Path, help="выходной native PRN")
    command.add_argument("--manifest", type=Path, help="выходной RIP manifest")
    command.add_argument("--result", type=Path, help="выходной PSA render-result JSON")
    command.set_defaults(phase8_handler=_psa_render_job)
    command = subparsers.add_parser("list-printers", help="показать реально зарегистрированные модели")
    command.set_defaults(phase8_handler=_list_printers)
    command = subparsers.add_parser("import-printer", help="импортировать один Epson PPD + Gutenprint model")
    command.add_argument("ppd", type=Path, help="локальный Epson ESC/P-R PPD")
    command.add_argument("--gutenprint-root", type=Path, help="корень Gutenprint source tree")
    command.add_argument("--gutenprint-model", help="явный Gutenprint model ID")
    command.add_argument("--dry-run", action="store_true", help="показать нормализацию без записи")
    command.add_argument("--force", action="store_true", help="заменить существующее imported definition")
    command.set_defaults(phase8_handler=_import_printer)
    command = subparsers.add_parser(
        "import-printer-support",
        help="импортировать поддержку одной подготовленной модели Epson",
    )
    command.add_argument("printer", help="точный model ID, display name или package ID")
    command.add_argument("--unified-root", type=Path)
    command.add_argument("--qtr-root", type=Path)
    command.add_argument("--catalog-root", type=Path)
    command.add_argument(
        "--device-settings-root",
        type=Path,
        help="каталог исходных Epson model описателей с полными [Setting*] секциями",
    )
    command.add_argument("--hidden", action="store_true", help="не показывать модель в GUI")
    command.add_argument("--force", action="store_true", help="обновить существующий package")
    command.set_defaults(phase8_handler=_import_printer_support)
    command = subparsers.add_parser(
        "import-supported-printers",
        help="импортировать весь подготовленный список Epson",
    )
    command.add_argument("--unified-root", type=Path)
    command.add_argument("--qtr-root", type=Path)
    command.add_argument("--catalog-root", type=Path)
    command.add_argument(
        "--device-settings-root",
        type=Path,
        help="каталог исходных Epson описателей с полными [Setting*] секциями",
    )
    command.add_argument("--hidden", action="store_true")
    command.add_argument("--force", action="store_true")
    command.set_defaults(phase8_handler=_import_supported_printers)
    command = subparsers.add_parser(
        "list-printer-catalog", help="показать полный импортированный catalog"
    )
    command.add_argument("--catalog-root", type=Path)
    command.set_defaults(phase8_handler=_list_printer_catalog)
    for command_name, visible, help_text in (
        ("show-printer-support", True, "показывать импортированную модель в GUI"),
        ("hide-printer-support", False, "скрыть модель в GUI без удаления реализации"),
    ):
        command = subparsers.add_parser(command_name, help=help_text)
        command.add_argument("printer", help="точный model ID или package ID")
        command.add_argument("--catalog-root", type=Path)
        command.set_defaults(
            phase8_handler=_set_printer_support_visibility,
            catalog_visible=visible,
        )
    command = subparsers.add_parser(
        "remove-printer-support",
        help="полностью удалить один точный импортированный package",
    )
    command.add_argument("printer", help="точный model ID или package ID")
    command.add_argument("--catalog-root", type=Path)
    command.add_argument(
        "--confirm",
        action="store_true",
        help="выполнить удаление; без флага команда показывает dry-run",
    )
    command.set_defaults(phase8_handler=_remove_printer_support)
    command = subparsers.add_parser("show-printer", help="показать capabilities модели")
    command.add_argument("printer", help="ID модели")
    command.set_defaults(phase8_handler=_show_printer)
    for name, help_text, handler in (
        ("list-modes", "показать source-derived режимы и их status", _list_modes),
        ("list-media", "показать PPD media и backend status", _list_media),
        ("list-pages", "показать PPD pages и printable regions", _list_pages),
    ):
        command = subparsers.add_parser(name, help=help_text)
        _add_printer_selector(command)
        command.add_argument("printer_id", nargs="?", help="ID модели принтера")
        command.set_defaults(phase8_handler=handler)
    command = subparsers.add_parser(
        "inspect-profile", help="исследовать compiled .quad"
    )
    _add_profile_arguments(command, positional=True)
    _add_printer_selector(command)
    command.set_defaults(phase8_handler=_inspect_profile)
    command = subparsers.add_parser(
        "validate-profile", help="строго проверить compiled .quad"
    )
    _add_profile_arguments(command, positional=True)
    _add_printer_selector(command)
    command.set_defaults(phase8_handler=_validate_profile)
    command = subparsers.add_parser("inspect-job", help="полностью разобрать native PRN без печати")
    command.add_argument("path", type=Path)
    command.set_defaults(phase8_handler=_inspect_job)
    command = subparsers.add_parser("show-config", help="прочитать и нормализовать TOML")
    command.add_argument("path", type=Path)
    command.set_defaults(phase8_handler=_show_config)
    command = subparsers.add_parser(
        "trace-runtime", help="численная трассировка EpsonRIP QTR runtime controls"
    )
    _add_profile_arguments(command)
    command.add_argument("--curve1", type=Path)
    command.add_argument("--curve2", type=Path)
    command.add_argument("--curve3", type=Path)
    command.add_argument("--blend")
    command.add_argument("--split-tone", action=argparse.BooleanOptionalAction, default=None)
    command.add_argument("--blend-highlights")
    command.add_argument("--blend-midtones")
    command.add_argument("--blend-shadows")
    command.add_argument("--gamma", type=int)
    command.add_argument("--ink-limit", type=int)
    command.add_argument("--config", type=Path)
    command.add_argument(
        "--codes",
        default="0,16,32,64,96,128,160,192,224,255",
        help="QTR codes через запятую",
    )
    command.set_defaults(phase8_handler=_trace_runtime)
    command = subparsers.add_parser(
        "render", help="создать и проверить PRN; transport никогда не открывается"
    )
    _add_job_arguments(command, allow_send=False)
    command.set_defaults(phase8_handler=_render)
    command = subparsers.add_parser(
        "print", help="создать/проверить PRN; передать только с явным --send"
    )
    _add_job_arguments(command, allow_send=True)
    command.set_defaults(phase8_handler=_print)


def _registry() -> PrinterRegistry:
    return default_registry()


def _psa_export_capabilities(args: argparse.Namespace) -> None:
    from xp750_rip.windows_psa import write_capability_export

    write_capability_export(args.output, _registry())
    print(args.output.resolve())


def _psa_export_pdc(args: argparse.Namespace) -> None:
    from xp750_rip.windows_psa import (
        write_model_print_device_capabilities,
        write_print_device_capabilities,
    )

    registry = _registry()
    write_print_device_capabilities(
        args.output, registry, fallback_only=args.fallback_only
    )
    if args.model_output_dir is not None:
        write_model_print_device_capabilities(args.model_output_dir, registry)
    print(args.output.resolve())


def _psa_bridge_job(args: argparse.Namespace) -> None:
    from xp750_rip.windows_psa import write_bridge_result

    result = write_bridge_result(args.record, args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def _psa_render_job(args: argparse.Namespace) -> None:
    payload = render_psa_job(
        args.record,
        input_tiff=args.input_tiff,
        curve_root=args.curve_root,
        normalized_tiff=args.normalized_tiff,
        output_prn=args.output_prn,
        manifest=args.manifest,
        result_path=args.result,
        performance_trace=getattr(args, "performance_trace", None),
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _replay_print_copies(
    source: Path,
    copies: int,
    send_chunk: Callable[[bytes], None],
    *,
    cancel_check: Callable[[], None] | None = None,
) -> int:
    """Send additional complete native jobs from the captured first copy."""

    if not 1 <= copies <= 99:
        raise ValueError("JobCopiesAllDocuments must be in 1..99")
    size = source.stat().st_size
    if size <= 0:
        raise ValueError("cannot repeat an empty native print job")
    for _ in range(1, copies):
        with source.open("rb") as stream:
            remaining = size
            while remaining:
                if cancel_check is not None:
                    cancel_check()
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise OSError("native print job changed during copy replay")
                send_chunk(chunk)
                remaining -= len(chunk)
    return size * (copies - 1)


def render_psa_job(
    record: Path,
    *,
    output_prn: Path,
    input_tiff: Path | None = None,
    curve_root: Path | None = None,
    normalized_tiff: Path | None = None,
    manifest: Path | None = None,
    result_path: Path | None = None,
    performance_trace: PerformanceTrace | None = None,
    live_transport: StreamingTransport | None = None,
    cancel_check: Callable[[], None] | None = None,
    send_progress: Callable[[int], None] | None = None,
    send_total_bytes: Callable[[int], None] | None = None,
) -> dict[str, object]:
    """Render a PSA job in strict offline or opt-in live delivery mode."""

    from xp750_rip.cli import build_parser
    from xp750_rip.windows_psa import (
        PDL_ADAPTER_TIFF_TOKEN,
        bridge_job_record,
        normalize_adapter_tiff,
        print_ticket_resolution,
        write_render_result,
    )

    bridge = bridge_job_record(record, curve_root=curve_root)
    copies = int(bridge["copies"])
    adapter_tiff = input_tiff
    if adapter_tiff is None and bridge.get("adapter_tiff_file"):
        adapter_tiff = Path(str(bridge["adapter_tiff_file"]))
    if adapter_tiff is None:
        raise ValueError("job record не содержит adapter_tiff; укажите --input-tiff")
    normalized_tiff = normalized_tiff or (
        record.resolve().parent / "python-input-grayscale.tiff"
    )
    ticket_dpi = print_ticket_resolution(Path(str(bridge["print_ticket_file"])))
    rgb_calibration = "rgb_selectors" in bridge["render_argument_tokens"]
    if cancel_check is not None:
        cancel_check()
    if rgb_calibration:
        # Selector bytes are the calibration protocol.  Converting this TIFF
        # to L would irreversibly discard both the selector and the coverage G.
        normalized_tiff = adapter_tiff
    else:
        normalize_adapter_tiff(adapter_tiff, normalized_tiff, dpi=ticket_dpi)
    if cancel_check is not None:
        cancel_check()
    tokens = [
        str(normalized_tiff) if item == PDL_ADAPTER_TIFF_TOKEN else item
        for item in bridge["render_argument_tokens"]
    ]
    tokens.extend(("--output", str(output_prn)))
    if manifest is not None:
        tokens.extend(("--manifest", str(manifest)))
    render_args = build_parser().parse_args(tokens)
    if performance_trace is not None:
        render_args.performance_trace = performance_trace
    job, transport = _resolve_job(render_args)
    if transport is not None:
        raise AssertionError("psa-render-job must never create a transport")
    if live_transport is not None:
        output_prn.parent.mkdir(parents=True, exist_ok=True)
        if output_prn.exists():
            raise FileExistsError(f"Refusing to overwrite existing PRN: {output_prn}")
        partial = output_prn.with_name(output_prn.name + ".partial")
        digest = sha256()
        live_summary: LiveRenderSummary | None = None
        with partial.open("xb") as stream:
            def produce(send_chunk: Callable[[bytes], None]) -> None:
                nonlocal live_summary

                def write_chunk(chunk: bytes) -> None:
                    stream.write(chunk)
                    digest.update(chunk)
                    send_chunk(chunk)

                rendered = MonoRipService().render(
                    job, write_chunk=write_chunk, cancel_check=cancel_check
                )
                assert isinstance(rendered, LiveRenderSummary)
                live_summary = rendered
                stream.flush()
                if stream.tell() != live_summary.stream_size:
                    raise ValueError("Captured PRN differs from the first native copy")
                if send_total_bytes is not None:
                    send_total_bytes(live_summary.stream_size * copies)
                _replay_print_copies(
                    partial, copies, send_chunk, cancel_check=cancel_check
                )

            receipt = live_transport.send_from_producer(
                produce, cancel_check=cancel_check, progress=send_progress
            )
            stream.flush()
        assert live_summary is not None
        if partial.stat().st_size * copies != receipt.bytes_sent:
            raise ValueError("Live PRN copy count and transmitted byte count differ")
        if live_summary.stream_size != partial.stat().st_size or live_summary.stream_sha256 != digest.hexdigest():
            raise ValueError("Live RIP and transport stream accounting differ")
        partial.rename(output_prn)
        if manifest is not None:
            value = live_summary.manifest
            value["printer_access_performed_during_render"] = True
            value["network_access_performed_during_render"] = isinstance(live_transport, RawTcpTransport)
            value["transmission"] = {
                "status": "SENT",
                "bytes_sent": receipt.bytes_sent,
                "destination": receipt.destination,
                "copies": copies,
            }
            manifest.parent.mkdir(parents=True, exist_ok=True)
            manifest.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        payload = write_render_result(
            bridge=bridge,
            input_tiff=normalized_tiff,
            adapter_tiff=adapter_tiff,
            prn_path=output_prn,
            manifest_path=manifest,
            output=None,
        )
        payload["status"] = "SENT"
        payload["transport_attempted"] = True
        payload["transport"] = {
            "destination": receipt.destination,
            "bytes_sent": receipt.bytes_sent,
            "copies": copies,
        }
        if result_path is not None:
            result_path.parent.mkdir(parents=True, exist_ok=True)
            result_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return payload
    execution = MonoRipService().execute(job, send=False, transport=None)
    return write_render_result(
        bridge=bridge,
        input_tiff=normalized_tiff,
        adapter_tiff=adapter_tiff,
        prn_path=execution.prn_path,
        manifest_path=execution.manifest_path,
        output=result_path,
    )


def _selected_model(args: argparse.Namespace) -> PrinterModel:
    identity = getattr(args, "printer_id", None) or args.printer or "XP750"
    return _registry().get(identity)


def _status(flag: bool) -> str:
    return "YES" if flag else "NO"


def _list_printers(args: argparse.Namespace) -> None:
    del args
    for model in _registry().models:
        implemented = any(mode.implemented for mode in model.modes)
        print(
            f"{model.identity}\t{model.display_name}\tpaths={len(model.physical_paths)}\t"
            f"IMPLEMENTED={_status(implemented)}"
        )


def _psa_generate_package_assets(args: argparse.Namespace) -> None:
    from xp750_rip.windows_package_assets import generate_windows_package_assets

    catalog = generate_windows_package_assets(
        args.output_dir, version=args.version, registry=_registry()
    )
    print(
        f"Сгенерированы Windows package assets: {len(catalog['modules'])} моделей; "
        f"catalog={args.output_dir / 'catalog.json'}"
    )


def _import_printer(args: argparse.Namespace) -> None:
    definition = normalize_import(
        args.ppd,
        gutenprint_root=args.gutenprint_root or GUTENPRINT_ROOT,
        override_model=args.gutenprint_model,
    )
    print(json.dumps(import_report(definition), ensure_ascii=False, indent=2))
    if args.dry_run:
        print("Режим dry-run: определение не сохранено")
        return
    if definition.printer_id.casefold() in {item.identity.casefold() for item in (load_xp750_model(), load_xp960_model())}:
        raise ValueError(
            f"Нельзя заменить встроенное определение {definition.printer_id}; "
            "используйте отдельный printer_id/PPD или не импортируйте built-in"
        )
    path = persist_definition(definition, directory=DEFINITION_DIR, force=args.force)
    print(f"Сохранено: {path}")


def _catalog_arguments(args: argparse.Namespace) -> tuple[Path, Path]:
    from xp750_rip.printers.catalog import (
        DEFAULT_UNIFIED_ROOT,
        DEVELOPMENT_CATALOG_ROOT,
    )

    return (
        args.unified_root or DEFAULT_UNIFIED_ROOT,
        args.catalog_root or DEVELOPMENT_CATALOG_ROOT,
    )


def _import_printer_support(args: argparse.Namespace) -> None:
    from xp750_rip.printers.catalog import (
        DEFAULT_QTR_ROOT,
        import_unified_package,
        resolve_supported_package,
    )

    unified_root, catalog_root = _catalog_arguments(args)
    package_id = resolve_supported_package(
        args.printer,
        unified_root=unified_root,
        qtr_root=args.qtr_root or DEFAULT_QTR_ROOT,
    )
    imported = import_unified_package(
        package_id,
        unified_root=unified_root,
        catalog_root=catalog_root,
        device_settings_root=args.device_settings_root,
        visible=not args.hidden,
        force=args.force,
    )
    print(json.dumps(asdict(imported), ensure_ascii=False, indent=2, default=list))


def _import_supported_printers(args: argparse.Namespace) -> None:
    from xp750_rip.printers.catalog import (
        DEFAULT_QTR_ROOT,
        discover_supported_packages,
        import_unified_package,
    )

    unified_root, catalog_root = _catalog_arguments(args)
    qtr_root = args.qtr_root or DEFAULT_QTR_ROOT
    packages = discover_supported_packages(unified_root, qtr_root)
    completed = []
    for package_id in packages:
        completed.append(
            import_unified_package(
                package_id,
                unified_root=unified_root,
                catalog_root=catalog_root,
                device_settings_root=args.device_settings_root,
                visible=not args.hidden,
                force=args.force,
            )
        )
    print(f"Импортировано Epson packages: {len(completed)}")
    for item in completed:
        print(f"{item.model_id}\t{item.package_id}\t{item.backend_status}")


def _list_printer_catalog(args: argparse.Namespace) -> None:
    from xp750_rip.printers.catalog import DEVELOPMENT_CATALOG_ROOT, load_catalog_packages

    root = args.catalog_root or DEVELOPMENT_CATALOG_ROOT
    for item in load_catalog_packages(root):
        print(
            f"{item.model_id}\t{item.package_id}\tvisible={'YES' if item.visible else 'NO'}\t"
            f"backend={item.backend_status}\tppd={item.ppd_source}"
        )


def _set_printer_support_visibility(args: argparse.Namespace) -> None:
    from xp750_rip.printers.catalog import DEVELOPMENT_CATALOG_ROOT, set_package_visibility

    item = set_package_visibility(
        args.printer,
        bool(args.catalog_visible),
        root=args.catalog_root or DEVELOPMENT_CATALOG_ROOT,
    )
    print(f"{item.model_id}: visible={'YES' if item.visible else 'NO'}")


def _remove_printer_support(args: argparse.Namespace) -> None:
    from xp750_rip.printers.catalog import DEVELOPMENT_CATALOG_ROOT, remove_imported_package

    dry_run = not args.confirm
    files = remove_imported_package(
        args.printer,
        root=args.catalog_root or DEVELOPMENT_CATALOG_ROOT,
        dry_run=dry_run,
    )
    print("DRY-RUN; ничего не удалено" if dry_run else "Поддержка принтера удалена")
    for path in files:
        print(path)


def _show_printer(args: argparse.Namespace) -> None:
    model = _registry().get(args.printer)
    print(f"Модель: {model.identity} — {model.display_name}")
    print(f"Backend: {model.backend_family}")
    print(f"PhysicalPaths ({len(model.physical_paths)}): " + ", ".join(
        f"{item.identity.value} ({item.factory_role or 'UNKNOWN'})" for item in model.physical_paths
    ))
    print(f"Modes: {len(model.modes)}; media: {len(model.media)}; pages: {len(model.pages)}")
    print(
        "Borderless: DISCOVERED/NOT_IMPLEMENTED (native overspray и edge feed не завершены)"
    )
    print("Modes:")
    for item in model.modes:
        print(
            f"  {item.identity}: implemented={_status(item.implemented)}, "
            f"offline={_status(item.offline_validated)}, "
            f"matrix={item.ordered_matrix_id}, hpasses={item.horizontal_passes}, "
            f"vpasses={item.vertical_passes}"
        )
    print("Media:")
    for item in model.media:
        print(
            f"  {item.identity}: implemented={_status(item.implemented)}, "
            f"model96={item.gutenprint_name or 'UNKNOWN'}"
        )
    print("Pages:")
    for item in model.pages:
        print(
            f"  {item.ppd.name}: implemented={_status(item.implemented)}, "
            f"borderless={_status(item.ppd.borderless)}"
        )
    print("Input sources:")
    for item in model.input_sources:
        print(f"  {item.identity}: implemented={_status(item.implemented)}")
    print("Источники:")
    for source in model.source_summary:
        print(f"  {source}")


def _list_modes(args: argparse.Namespace) -> None:
    model = _selected_model(args)
    print(f"Модель: {model.identity} — {model.display_name}")
    for mode in model.modes:
        print(
            f"{mode.identity}\t{mode.horizontal_dpi}x{mode.vertical_dpi} dpi\t"
            f"physical={mode.physical_horizontal_dpi}x{mode.physical_vertical_dpi}\t"
            f"hpasses={mode.horizontal_passes}\tvpasses={mode.vertical_passes}\t"
            f"matrix={mode.ordered_matrix_id}\tdrops={','.join(str(x) for x in mode.drop_sizes)}\t"
            f"IMPLEMENTED={_status(mode.implemented)}\t"
            f"OFFLINE_VALIDATED={_status(mode.offline_validated)}\t{mode.source}"
        )
        if mode.blocked_reason:
            print(f"  BLOCKED: {mode.blocked_reason}")


def _list_media(args: argparse.Namespace) -> None:
    model = _selected_model(args)
    print(f"Модель: {model.identity} — {model.display_name}")
    for medium in model.media:
        print(
            f"{medium.identity}\t{medium.display_name}\t"
            f"model96={medium.gutenprint_name or 'UNKNOWN'}\t"
            f"PaperMedia={medium.media_code if medium.media_code is not None else 'UNKNOWN'}\t"
            f"modes={','.join(medium.supported_modes) if medium.supported_modes else '-'}\t"
            f"IMPLEMENTED={_status(medium.implemented)}"
        )
        if medium.blocked_reason:
            print(f"  BLOCKED: {medium.blocked_reason}")


def _list_pages(args: argparse.Namespace) -> None:
    model = _selected_model(args)
    print(f"Модель: {model.identity} — {model.display_name}")
    for page in model.pages:
        ppd = page.ppd
        print(
            f"{ppd.name}\t{ppd.width_mm:.2f}x{ppd.height_mm:.2f} mm\t"
            f"printable={ppd.printable_width_points * 25.4 / 72:.2f}x"
            f"{ppd.printable_height_points * 25.4 / 72:.2f} mm\t"
            f"borderless={_status(ppd.borderless)}\tIMPLEMENTED={_status(page.implemented)}"
        )
        if page.blocked_reason:
            print(f"  BLOCKED: {page.blocked_reason}")


def _bundle_from_args(args: argparse.Namespace) -> QtrProfileBundle:
    quad = args.quad or getattr(args, "quad_option", None)
    if quad is None:
        raise ValueError(
            "inspect-profile/validate-profile требуют путь к .quad: "
            "укажите позиционный путь или --quad"
        )
    return load_profile_bundle(quad_path=quad)


def _profile_report(
    bundle: QtrProfileBundle, target_printer: str | None = None
) -> dict[str, object]:
    return {
        "logical_ink_count": len(bundle.logical_inks),
        "logical_inks": list(bundle.logical_inks),
        "curve_input_domain": [0, 255],
        "curve_output_range": [0, 65535],
        "curve_sample_count": 256,
        "curves": {
            curve.ink_code: {
                "minimum": min(curve.samples),
                "maximum": max(curve.samples),
                "nonzero_nodes": sum(value != 0 for value in curve.samples),
            }
            for curve in bundle.quad.curves
        },
        "quad_version": bundle.quad.quadprofile_version,
        "quad_sha256": bundle.quad_sha256,
        "target_printer": target_printer,
        "warnings": list(bundle.warnings),
    }


def _inspect_profile(args: argparse.Namespace) -> None:
    print(
        json.dumps(
            _profile_report(_bundle_from_args(args), args.printer),
            ensure_ascii=False,
            indent=2,
        )
    )


def _validate_profile(args: argparse.Namespace) -> None:
    bundle = _bundle_from_args(args)
    report = _profile_report(bundle, args.printer)
    report["validation"] = "PASS"
    print(json.dumps(report, ensure_ascii=False, indent=2))


def _inspect_job(args: argparse.Namespace) -> None:
    data = args.path.read_bytes()
    records = tuple(parse_stream(data))
    if not records or records[-1].end_offset != len(data):
        raise ValueError("PRN не разобран до конца")
    print(f"Файл: {args.path.resolve()}")
    print(f"Размер: {len(data)} bytes")
    print(f"SHA-256: {sha256(data).hexdigest()}")
    print(format_stream(list(records)), end="")


def _show_config(args: argparse.Namespace) -> None:
    config = load_user_config(args.path)
    print(
        json.dumps(
            {"source": str(config.source_path), "sections": config.sections},
            ensure_ascii=False,
            indent=2,
        )
    )


def _config_or_empty(path: Path | None) -> UserConfig | None:
    return load_user_config(path) if path is not None else None


def _section(config: UserConfig | None, name: str) -> dict[str, object]:
    return config.section(name) if config is not None else {}


def _pick(cli_value: Any, config: dict[str, object], key: str, default: Any) -> Any:
    return cli_value if cli_value is not None else config.get(key, default)


def _parse_routes(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        logical, separator, path = value.partition("=")
        if not separator or not logical.strip() or not path.strip():
            raise ValueError(f"Некорректный --map {value!r}; требуется LOGICAL=PATH")
        result[logical.strip()] = path.strip()
    return result


def _parse_device_settings(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        setting, separator, choice = value.partition("=")
        if not separator or not setting.strip() or not choice.strip():
            raise ValueError(
                f"Invalid --device-setting {value!r}; expected SETTING=CHOICE"
            )
        if setting.strip() in result:
            raise ValueError(f"Device setting was specified twice: {setting.strip()}")
        result[setting.strip()] = choice.strip()
    return result


def _parse_weights(value: object, *, name: str) -> tuple[int, ...] | None:
    if value is None:
        return None
    raw_values: list[object]
    if isinstance(value, str):
        raw_values = [item.strip() for item in value.split(",") if item.strip()]
    elif isinstance(value, list):
        raw_values = value
    else:
        raise TypeError(f"{name} должен быть строкой или TOML array")
    values_list: list[int] = []
    for item in raw_values:
        if isinstance(item, bool):
            raise TypeError(f"{name} содержит нецелый weight")
        if isinstance(item, int):
            values_list.append(item)
        elif isinstance(item, str) and item.lstrip("-").isdigit():
            values_list.append(int(item))
        else:
            raise ValueError(f"{name} содержит нецелый weight")
    values = tuple(values_list)
    return values


def _integer_control(value: object, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} должен быть целым")
    return value


def _resolve_advanced_adjustments(
    args: argparse.Namespace,
    config: UserConfig | None,
) -> QtrAdvancedAdjustments:
    qtr = config.qtr if config is not None else {}
    raw_adjustments = qtr.get("adjustments", {})
    if not isinstance(raw_adjustments, dict):
        raise TypeError("[qtr.adjustments] должен быть TOML table")
    gamma = _pick(getattr(args, "gamma", None), raw_adjustments, "gamma", 0)
    ink_limit = _pick(
        getattr(args, "ink_limit", None), raw_adjustments, "ink_limit", 0
    )
    return QtrAdvancedAdjustments(
        _integer_control(gamma, name="gamma"),
        _integer_control(ink_limit, name="ink_limit"),
    )


def _config_runtime_profiles(
    config: UserConfig | None,
) -> list[Path | None]:
    if config is None:
        return []
    qtr = config.qtr
    raw_profiles = qtr.get("profiles")
    if raw_profiles is None:
        profile_section = config.section("profile")
        unknown_profile_keys = set(profile_section) - {"quad"}
        if unknown_profile_keys:
            raise ValueError(
                "[profile] содержит неподдерживаемые runtime keys: "
                + ", ".join(sorted(unknown_profile_keys))
            )
        quad = config.path_value("profile", "quad")
        return [quad] if quad is not None else []
    if not isinstance(raw_profiles, list) or not 1 <= len(raw_profiles) <= 3:
        raise ValueError("[[qtr.profiles]] должен содержать от 1 до 3 profiles")
    profiles: list[Path] = []
    for index, item in enumerate(raw_profiles, start=1):
        if not isinstance(item, dict):
            raise TypeError(f"qtr.profiles[{index}] должен быть TOML table")
        unknown = set(item) - {"path"}
        if unknown:
            raise ValueError(
                f"qtr.profiles[{index}] содержит неизвестные keys: {sorted(unknown)}"
            )
        if "path" not in item:
            raise ValueError(f"qtr.profiles[{index}] требует path")
        profiles.append(
            config.resolve_path(item["path"], context=f"qtr.profiles[{index}].path")
        )
    return profiles


def _resolve_qtr_runtime(
    args: argparse.Namespace,
    config: UserConfig | None,
    trace: PerformanceTrace | None,
) -> tuple[tuple[QtrProfileBundle, ...], QtrRuntimeProfileSet]:
    slots: list[Path | None] = _config_runtime_profiles(config)
    while len(slots) < 3:
        slots.append(None)
    quad = getattr(args, "quad", None)
    curve1 = getattr(args, "curve1", None)
    if quad is not None and curve1 is not None:
        raise ValueError("--quad и --curve1 обозначают один slot; укажите только один")
    first = curve1 or quad
    if first is not None:
        slots[0] = first
    for index, value in enumerate(
        (getattr(args, "curve2", None), getattr(args, "curve3", None)), start=1
    ):
        if value is not None:
            slots[index] = value
    active_slots = tuple(slot for slot in slots if slot is not None)
    if not active_slots:
        raise ValueError("Требуется --quad/--curve1 или [[qtr.profiles]]")
    bundles = tuple(
        load_profile_bundle(quad_path=path, trace=trace)
        for path in active_slots
    )

    qtr = config.qtr if config is not None else {}
    raw_blend = qtr.get("blend", {})
    if not isinstance(raw_blend, dict):
        raise TypeError("[qtr.blend] должен быть TOML table")
    split_tone = getattr(args, "split_tone", None)
    if split_tone is None:
        split_tone = bool(raw_blend.get("split_tone", False))
    global_weights = _parse_weights(
        getattr(args, "blend", None) or raw_blend.get("weights"), name="blend weights"
    )
    count = len(bundles)
    if global_weights is None and count == 1:
        global_weights = (100,)
    if not split_tone:
        if global_weights is None:
            raise ValueError("Несколько profiles требуют --blend или qtr.blend.weights")
        tone_weights = QtrToneBlendWeights(
            global_weights, global_weights, global_weights
        )
    else:
        highlights = _parse_weights(
            getattr(args, "blend_highlights", None) or raw_blend.get("highlights"),
            name="highlight weights",
        ) or global_weights
        midtones = _parse_weights(
            getattr(args, "blend_midtones", None) or raw_blend.get("midtones"),
            name="midtone weights",
        ) or global_weights
        shadows = _parse_weights(
            getattr(args, "blend_shadows", None) or raw_blend.get("shadows"),
            name="shadow weights",
        ) or global_weights
        if highlights is None or midtones is None or shadows is None:
            raise ValueError("Split Tone требует H/M/S weights или global weights")
        tone_weights = QtrToneBlendWeights(highlights, midtones, shadows)

    runtime = QtrRuntimeProfileSet(
        tuple(bundle.quad for bundle in bundles),
        QtrBlendConfiguration(bool(split_tone), tone_weights),
        _resolve_advanced_adjustments(args, config),
    )
    return bundles, runtime


def _trace_runtime(args: argparse.Namespace) -> None:
    config = _config_or_empty(args.config)
    bundles, runtime = _resolve_qtr_runtime(args, config, None)
    codes = _parse_weights(args.codes, name="codes")
    assert codes is not None
    if any(code < 0 or code > 255 for code in codes):
        raise ValueError("--codes должны быть в диапазоне 0..255")
    entries: list[dict[str, object]] = []
    for code in codes:
        item = trace_runtime_code(code, runtime)
        entries.append(
            {
                "input_tiff_sample": 255 - code,
                "qtr_code": code,
                "gamma_adjusted_code": item.gamma_adjusted_code,
                "profile_vectors": [
                    dict(zip(runtime.logical_inks, vector, strict=True))
                    for vector in item.profile_vectors
                ],
                "blend_weight_numerators": list(item.weight_numerators),
                "blend_weight_denominator": item.weight_denominator,
                "blended_vector": dict(
                    zip(runtime.logical_inks, item.blended_vector, strict=True)
                ),
                "limit_adjusted_final_vector": dict(
                    zip(runtime.logical_inks, item.final_vector, strict=True)
                ),
            }
        )
    print(
        json.dumps(
            {
                "model": "EpsonRIP-defined transparent runtime v1",
                "profiles": [str(bundle.quad_path) for bundle in bundles],
                "logical_inks": list(runtime.logical_inks),
                "entries": entries,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def _resolve_job(args: argparse.Namespace) -> tuple[PrintJobConfiguration, RawTcpTransport | None]:
    trace: PerformanceTrace | None = getattr(args, "performance_trace", None)
    if trace is None:
        config = _config_or_empty(args.config)
        mapping_config = _config_or_empty(args.mapping)
    else:
        with trace.stage("config file parsing"):
            config = _config_or_empty(args.config)
            mapping_config = _config_or_empty(args.mapping)
    print_section = _section(config, "print")
    printer_section = _section(config, "printer")
    transport_section = _section(config, "transport")
    defaults_section = _section(config, "defaults")
    halftone_section = _section(config, "halftone")
    calibration_mode = getattr(args, "calibration_mode", None)
    if calibration_mode is None:
        bundles, runtime = _resolve_qtr_runtime(args, config, trace)
        bundle = bundles[0]
        calibration = None
    else:
        bundles, runtime, bundle = (), None, None
        calibration = CalibrationConfiguration(
            calibration_mode,
            _integer_control(getattr(args, "calibration_ink_limit", None), name="calibration_ink_limit")
            if getattr(args, "calibration_ink_limit", None) is not None else 100,
            _resolve_advanced_adjustments(args, config),
        )
    printer_id = _pick(
        args.printer,
        printer_section,
        "model",
        defaults_section.get("printer", "XP750"),
    )
    model = _registry().get(str(printer_id))
    ink_configuration = _pick(
        getattr(args, "ink_configuration", None),
        printer_section,
        "ink_configuration",
        model.default_ink_configuration,
    )
    try:
        model.ink_configuration(
            str(ink_configuration) if ink_configuration is not None else None
        )
    except KeyError as error:
        raise ValueError(
            f"Unknown ink configuration for {model.identity}: {ink_configuration}"
        ) from error
    mode_id = str(_pick(args.mode, print_section, "mode", defaults_section.get("mode", "360x180sw")))
    media_id = str(
        _pick(args.media, print_section, "media", defaults_section.get("media", "PLAIN_NORMAL"))
    )
    page_id = str(_pick(args.page, print_section, "page", defaults_section.get("page", "A4")))
    input_source_id = str(
        _pick(
            args.input_source, print_section, "input_source",
            model.selectable_input_sources()[0].identity,
        )
    )
    try:
        mode = model.mode(mode_id)
    except KeyError as error:
        raise ValueError(f"PrinterMode не найден: {mode_id}") from error

    inks_value = str(_pick(args.inks, print_section, "inks", "all"))
    ink_names = (
        runtime.logical_inks
        if inks_value.casefold() == "all"
        else tuple(item.strip() for item in inks_value.split(",") if item.strip())
    )
    if not ink_names or len(ink_names) != len(set(ink_names)):
        raise ValueError("--inks должен содержать уникальные logical ink IDs")

    routes: dict[str, str] = {}
    if config:
        routes.update(config.routing)
    if mapping_config:
        routes.update(mapping_config.routing)
    routes.update(_parse_routes(args.map))
    router = ChannelRouter(
        {LogicalChannelId(key): PhysicalPathId(value) for key, value in routes.items()}
    )
    inkset: dict[str, str] = {}
    if config:
        inkset.update(config.inkset)
    if mapping_config:
        inkset.update(mapping_config.inkset)

    center_config = print_section.get("center")
    center = args.center if args.center is not None else bool(center_config if center_config is not None else True)
    if args.x_mm is not None or args.y_mm is not None:
        if args.center:
            raise ValueError("--center нельзя сочетать с --x-mm/--y-mm")
        center = False
    fit = bool(_pick(args.fit, print_section, "fit", False))
    native_size = bool(_pick(args.native_size, print_section, "native_size", False))
    size = ImageSizeRequest(
        width_mm=_pick(args.width_mm, print_section, "width_mm", None),
        height_mm=_pick(args.height_mm, print_section, "height_mm", None),
        scale_percent=_pick(args.scale_percent, print_section, "scale_percent", None),
        fit=fit,
        native_size=native_size,
        resampling=ResamplingMethod(
            str(_pick(args.resampling, print_section, "resampling", "lanczos"))
        ),
        orientation=LayoutOrientation(
            str(_pick(args.orientation, print_section, "orientation", "auto"))
        ),
        center=center,
        x_mm=_pick(args.x_mm, print_section, "x_mm", None),
        y_mm=_pick(args.y_mm, print_section, "y_mm", None),
        windows_page=bool(args.windows_page),
    )
    output_path = args.output or args.image.with_suffix(".prn")
    manifest_path = args.manifest
    instance = PrinterInstance(
        model=model,
        name=str(_pick(args.printer_name, printer_section, "name", model.display_name)),
        transport_type=str(transport_section.get("type", "raw_tcp")),
        host=str(transport_section["host"]) if "host" in transport_section else None,
        port=int(transport_section.get("port", 9100)),
    )
    device_settings = _parse_device_settings(args.device_setting)
    for setting_id, choice_id in device_settings.items():
        try:
            model.device_setting(setting_id).choice(choice_id)
        except KeyError as error:
            raise ValueError(
                f"Unknown device setting selection: {setting_id}={choice_id}"
            ) from error
    job = PrintJobConfiguration(
        source_image=args.image,
        profile_bundle=bundle,
        target=instance,
        mode=mode,
        media_id=media_id,
        page_id=page_id,
        input_source_id=input_source_id,
        image_size=size,
        active_logical_inks=tuple(LogicalChannelId(item) for item in ink_names),
        router=router,
        inkset=inkset,
        direction=parse_print_direction(
            _pick(args.direction, print_section, "direction", "bidirectional")
        ),
        allow_unsupported_ppd_combinations=args.allow_unsupported_ppd_combinations,
        runtime_profile_bundles=bundles,
        qtr_runtime=runtime,
        halftone=HalftoneConfiguration(
            algorithm=str(_pick(args.dither, halftone_section, "algorithm", "Ordered"))
        ),
        output=OutputConfiguration(output_path, manifest_path, args.diagnostics_dir),
        performance_trace=trace,
        calibration=calibration,
        device_settings=tuple(sorted(device_settings.items())),
        ink_configuration=(
            str(ink_configuration) if ink_configuration is not None else None
        ),
    )
    transport = None
    if getattr(args, "send", False):
        transport_type = str(transport_section.get("type", "raw_tcp"))
        if transport_type != "raw_tcp":
            raise ValueError(f"Неподдерживаемый transport type: {transport_type}")
        host = args.host or instance.host
        if host is None:
            raise ValueError("--send требует --host или [transport].host")
        transport = RawTcpTransport(
            host=host,
            port=int(args.port or instance.port),
            timeout_seconds=float(
                args.timeout
                or transport_section.get("timeout", DEFAULT_RAW_TCP_TIMEOUT_SECONDS)
            ),
        )
    return job, transport


def _execute_job(args: argparse.Namespace, *, send: bool) -> None:
    trace: PerformanceTrace | None = getattr(args, "performance_trace", None)
    resolution_started = perf_counter()
    if trace is not None:
        trace.event("job configuration resolution START")
    job, transport = _resolve_job(args)
    if trace is not None:
        trace.record_duration(
            "job configuration resolution", perf_counter() - resolution_started
        )
    result = MonoRipService().execute(job, send=send, transport=transport)
    print(f"PRN: {result.prn_path}")
    if result.manifest_path is not None:
        print(f"Manifest: {result.manifest_path}")
    if result.diagnostics_path is not None:
        print(f"Diagnostics: {result.diagnostics_path}")
    print(f"Размер: {len(result.rendered.data)} bytes")
    print("Native validation: PASS")
    print("Передача: SENT" if result.transmission else "Передача: NOT_SENT")


def _render(args: argparse.Namespace) -> None:
    _execute_job(args, send=False)


def _print(args: argparse.Namespace) -> None:
    _execute_job(args, send=bool(args.send))


def dispatch_product(args: argparse.Namespace) -> bool:
    handler = getattr(args, "phase8_handler", None)
    if handler is None:
        return False
    handler(args)
    return True
