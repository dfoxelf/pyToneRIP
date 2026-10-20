# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Product entry point; historical generators are isolated under ``dev``."""

from __future__ import annotations

import argparse
import sys
from importlib import import_module
from pathlib import Path
from time import perf_counter

from xp750_rip.performance import PerformanceTrace
from xp750_rip.product_cli import add_product_commands, dispatch_product

_DEVELOPMENT_EXPORTS = {
    "GOLDEN_SHA256",
    "make_phase3c_prn",
    "make_phase4_channel_tests",
    "make_phase5_multipath_tests",
    "make_test_prn",
}
__all__ = ["build_parser", "main"]
if Path(__file__).with_name("devtools_cli.py").is_file():
    __all__.extend(sorted(_DEVELOPMENT_EXPORTS))


def __getattr__(name: str):
    # Historical helpers remain available in the development checkout without
    # making the published product depend on local research generators.
    if name in _DEVELOPMENT_EXPORTS:
        return getattr(import_module("xp750_rip.devtools_cli"), name)
    raise AttributeError(name)


class RussianArgumentParser(argparse.ArgumentParser):
    """Минимальная локализация стандартных argparse заголовков и ошибок."""

    def format_help(self) -> str:
        return (
            super()
            .format_help()
            .replace("usage:", "использование:")
            .replace("positional arguments:", "позиционные аргументы:")
            .replace("options:", "параметры:")
            .replace("show this help message and exit", "показать справку и выйти")
        )

    def format_usage(self) -> str:
        return super().format_usage().replace("usage:", "использование:")

    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: ошибка: {message}\n")


def build_parser() -> argparse.ArgumentParser:
    parser = RussianArgumentParser(
        prog="epson-rip",
        description="Generic N-channel QTR-совместимый монохромный RIP",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    add_product_commands(subparsers)
    if Path(__file__).with_name("devtools_cli.py").is_file():
        add_dev_commands = import_module("xp750_rip.devtools_cli").add_dev_commands
        dev = subparsers.add_parser("dev", help="исторические generators и diagnostics")
        add_dev_commands(dev.add_subparsers(dest="dev_command", required=True))
    return parser


def _performance_log_path(argv: list[str]) -> Path | None:
    for index, value in enumerate(argv):
        if value == "--performance-log" and index + 1 < len(argv):
            return Path(argv[index + 1])
        if value.startswith("--performance-log="):
            return Path(value.partition("=")[2])
    return None


def main(argv: list[str] | None = None) -> None:
    actual_argv = list(sys.argv[1:] if argv is None else argv)
    performance_path = _performance_log_path(actual_argv)
    trace = PerformanceTrace(performance_path) if performance_path is not None else None
    parse_started = perf_counter()
    parser = build_parser()
    args = parser.parse_args(actual_argv)
    if trace is not None:
        args.performance_trace = trace
        trace.record_duration("CLI argument parsing", perf_counter() - parse_started)
    status = "ERROR"
    try:
        if dispatch_product(args):
            status = "PASS"
            return
        if args.command == "dev" and import_module("xp750_rip.devtools_cli").dispatch_dev(args):
            status = "PASS"
            return
        parser.error(f"Неизвестная команда: {args.command}")
    except (KeyError, OSError, TypeError, ValueError) as error:
        parser.error(str(error))
    finally:
        if trace is not None:
            trace.finish(status=status)
            trace.close()


if __name__ == "__main__":
    main()
