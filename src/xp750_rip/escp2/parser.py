# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Совместимый интерфейс поверх общего офлайн-парсера Phase 3B."""

from dataclasses import dataclass

from xp750_rip.analysis.escp2_parser import StreamRecord, parse_stream


@dataclass(frozen=True, slots=True)
class ParsedCommand:
    offset: int
    name: str
    details: str
    payload_length: int = 0


def _details(record: StreamRecord) -> str:
    if record.raster is not None:
        raster = record.raster
        return (
            f"selector={raster.selector} compression={raster.compression} "
            f"depth={raster.bit_depth} bytes_per_row={raster.bytes_per_row} rows={raster.rows}"
        )
    return " ".join(f"{key}={value}" for key, value in record.parameters.items())


def parse(data: bytes) -> list[ParsedCommand]:
    """Сохранить прежний API Phase 2, используя полный структурный разбор."""

    commands: list[ParsedCommand] = []
    for record in parse_stream(data):
        name = "PREINIT" if record.name == "PACKET_MODE_EXIT" else record.name
        commands.append(ParsedCommand(record.offset, name, _details(record), record.payload_length))
    return commands


def format_analysis(commands: list[ParsedCommand]) -> str:
    lines = ["СМЕЩЕНИЕ  КОМАНДА                 ДАННЫЕ   ПАРАМЕТРЫ"]
    for item in commands:
        lines.append(
            f"0x{item.offset:06X}  {item.name:<23} {item.payload_length:>7}  {item.details}"
        )
    return "\n".join(lines) + "\n"
