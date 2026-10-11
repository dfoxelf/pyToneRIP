# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Структурный офлайн-парсер исследуемого потока ESC/P2.

Парсер намеренно отделён от кодировщика. Он сохраняет исходные байты каждой
записи, а неподтверждённые конструкции помечает как ``UNKNOWN`` вместо того,
чтобы приписывать им семантику.
"""

from dataclasses import dataclass
from enum import StrEnum
from struct import unpack_from

from xp750_rip.escp2.packbits import decode_rows as packbits_decode_rows

type ParameterValue = int | str | bool | tuple[int, ...]


class EvidenceClass(StrEnum):
    """Классификация уверенности, принятая в проекте."""

    FACT = "FACT"
    INFERENCE = "INFERENCE"
    UNKNOWN = "UNKNOWN"


class ParseError(ValueError):
    """Поток оборван там, где граница записи известна из его структуры."""


@dataclass(frozen=True, slots=True)
class RasterData:
    """Заголовок ESC i, закодированные строки и их декодированное содержимое."""

    selector: int
    compression: int
    bit_depth: int
    bytes_per_row: int
    rows: int
    compressed_payload: bytes
    decoded_rows: tuple[bytes, ...]
    encoded_row_lengths: tuple[int, ...]

    @property
    def decoded_payload(self) -> bytes:
        return b"".join(self.decoded_rows)

    @property
    def pixels_per_row(self) -> int | None:
        if self.bit_depth == 0 or self.bytes_per_row % self.bit_depth:
            return None
        return self.bytes_per_row * 8 // self.bit_depth


@dataclass(frozen=True, slots=True)
class StreamRecord:
    """Одна структурно ограниченная запись исходного потока."""

    offset: int
    end_offset: int
    family: str
    name: str
    raw: bytes
    parameters: dict[str, ParameterValue]
    confidence: EvidenceClass
    evidence: str
    raster: RasterData | None = None

    @property
    def payload_length(self) -> int:
        if self.raster is not None:
            return len(self.raster.compressed_payload)
        value = self.parameters.get("payload_length", 0)
        return value if isinstance(value, int) else 0


_EXTENDED_NAMES = {
    b"G": "GRAPHICS_MODE",
    b"U": "UNIT_SETUP",
    b"K": "GRAY_MODE",
    b"i": "MICROWEAVE",
    b"e": "DOT_SIZE",
    b"D": "RESOLUTION",
    b"C": "PAGE_LENGTH",
    b"c": "PAGE_FORMAT",
    b"S": "PAPER_DIMENSIONS",
    b"m": "PRINT_METHOD",
    b"s": "LEGACY_MODE",
    b"v": "RELATIVE_VERTICAL",
    b"V": "ABSOLUTE_VERTICAL",
    b"$": "ABSOLUTE_HORIZONTAL",
}

_REMOTE_NAMES = {
    b"IR": "REMOTE_IR",
    b"EX": "REMOTE_EX",
    b"DP": "REMOTE_DP",
    b"FP": "REMOTE_FP",
    b"PM": "REMOTE_PM",
    b"JS": "REMOTE_JS",
    b"SN": "REMOTE_SN",
    b"PP": "REMOTE_PP",
    b"MI": "REMOTE_MI",
    b"IK": "REMOTE_IK",
    b"LD": "REMOTE_LD",
    b"JE": "REMOTE_JE",
    # Source-derived Epson REMOTE1 commands used by imported family profiles
    # and optional device settings from imported Epson descriptors.
    b"DR": "REMOTE_DR",
    b"PH": "REMOTE_PH",
    b"AC": "REMOTE_AC",
    b"US": "REMOTE_US",
    b"LB": "REMOTE_LB",
    b"CO": "REMOTE_CO",
    b"CH": "REMOTE_CH",
    b"VI": "REMOTE_VI",
    b"NC": "REMOTE_NC",
}


def _require(data: bytes, start: int, length: int, description: str) -> None:
    if start + length > len(data):
        raise ParseError(f"{description}: offset 0x{start:X}, требуется {length} байт")


def _record(
    data: bytes,
    start: int,
    end: int,
    family: str,
    name: str,
    parameters: dict[str, ParameterValue] | None = None,
    confidence: EvidenceClass = EvidenceClass.FACT,
    evidence: str = "границы и параметры следуют из байтов потока",
    raster: RasterData | None = None,
) -> StreamRecord:
    return StreamRecord(
        offset=start,
        end_offset=end,
        family=family,
        name=name,
        raw=data[start:end],
        parameters=parameters or {},
        confidence=confidence,
        evidence=evidence,
        raster=raster,
    )


def _decode_extended_parameters(code: bytes, payload: bytes) -> dict[str, ParameterValue]:
    params: dict[str, ParameterValue] = {
        "payload_length": len(payload),
        "payload_hex": payload.hex(" "),
    }
    if code == b"G" and len(payload) == 1:
        params["enabled"] = bool(payload[0])
    elif code == b"U" and len(payload) == 5:
        params.update(
            {
                "page_unit": payload[0],
                "vertical_unit": payload[1],
                "horizontal_unit": payload[2],
                "base_unit": int.from_bytes(payload[3:5], "little"),
            }
        )
    elif code in {b"K", b"i", b"m", b"s"} and len(payload) == 1:
        params["value"] = payload[0]
    elif code == b"e" and len(payload) == 2:
        params.update({"reserved": payload[0], "dot_size": payload[1]})
    elif code == b"D" and len(payload) == 4:
        params.update(
            {
                "base_unit": int.from_bytes(payload[:2], "little"),
                "vertical_unit": payload[2],
                "horizontal_unit": payload[3],
            }
        )
    elif code in {b"C", b"v", b"V", b"$"} and len(payload) == 4:
        params["value"] = int.from_bytes(payload, "little")
    elif code == b"c" and len(payload) == 8:
        params.update(
            {
                "top": int.from_bytes(payload[:4], "little", signed=True),
                "bottom": int.from_bytes(payload[4:], "little", signed=True),
            }
        )
    elif code == b"S" and len(payload) == 8:
        params.update(
            {
                "width": int.from_bytes(payload[:4], "little"),
                "height": int.from_bytes(payload[4:], "little"),
            }
        )
    return params


def _decode_remote_parameters(code: bytes, payload: bytes) -> dict[str, ParameterValue]:
    params: dict[str, ParameterValue] = {
        "payload_length": len(payload),
        "payload_hex": payload.hex(" "),
    }
    if code == b"JS" and payload:
        params["leading_byte"] = payload[0]
        params["label"] = payload[1:].rstrip(b"\x00").decode("ascii", errors="replace")
    elif code == b"PP" and len(payload) == 3:
        params.update(
            {"leading_byte": payload[0], "path_type": payload[1], "path_number": payload[2]}
        )
    elif code == b"MI" and len(payload) == 4:
        params.update(
            {
                "leading_byte": payload[0],
                "media_format": payload[1],
                "media_type": payload[2],
                "media_size": payload[3],
            }
        )
    elif code == b"DR" and len(payload) == 4:
        selector = payload[1]
        raw_value = int.from_bytes(payload[2:4], "little")
        params.update(
            {
                "leading_byte": payload[0],
                "drying_selector": selector,
                "drying_value": raw_value,
                "drying_seconds": (
                    float(raw_value) if selector == 1 else raw_value / 1000.0
                ),
            }
        )
    elif code == b"CO" and len(payload) == 8:
        params.update(
            {
                "leading_byte": payload[0],
                "cutter_group": payload[1],
                "cut_operation": payload[2],
                "reserved": payload[3],
                "page_height": int.from_bytes(payload[4:8], "little"),
            }
        )
    elif code == b"SN" and len(payload) == 3:
        selector_names = {
            0: "feed_sequence",
            4: "feed_adjustment",
            5: "vacuum_intensity",
            0x84: "cutter_edge_trim",
        }
        params.update(
            {
                "leading_byte": payload[0],
                "setting_selector": payload[1],
                "setting_name": selector_names.get(payload[1], "printer_specific"),
                "value": payload[2],
            }
        )
    elif code == b"LB" and len(payload) == 3:
        params.update(
            {
                "leading_byte": payload[0],
                "roll_option": payload[1],
                "value": payload[2],
            }
        )
    elif code == b"PH" and len(payload) == 2:
        params.update(
            {
                "leading_byte": payload[0],
                # The wire value is model-specific. Some model descriptors
                # label choices in 0.1 mm steps, but that cannot be assumed
                # for every Epson family. / Не выдаём код за миллиметры без
                # явной таблицы конкретной модели.
                "thickness_code": payload[1],
            }
        )
    elif code == b"US" and len(payload) == 3:
        params.update(
            {
                "leading_byte": payload[0],
                "setting_selector": payload[1],
                "value": payload[2],
            }
        )
        if payload[1] == 1:
            params["platen_gap_code"] = payload[2]
    elif code == b"JE" and len(payload) == 1:
        params["value"] = payload[0]
    return params


def _parse_raster(data: bytes, start: int) -> tuple[StreamRecord, int]:
    _require(data, start, 9, "оборван заголовок ESC i")
    selector, compression, bit_depth = data[start + 2 : start + 5]
    bytes_per_row, rows = unpack_from("<HH", data, start + 5)
    payload_start = start + 9
    index = payload_start
    decoded_rows: list[bytes] = []
    encoded_lengths: list[int] = []

    if compression not in {0, 1}:
        return (
            _record(
                data,
                start,
                len(data),
                "ESC/P2_RASTER",
                "RASTER_IMAGE_UNKNOWN_COMPRESSION",
                {
                    "selector": selector,
                    "compression": compression,
                    "bit_depth": bit_depth,
                    "bytes_per_row": bytes_per_row,
                    "rows": rows,
                },
                EvidenceClass.UNKNOWN,
                "длина растра не выводится для неподдерживаемого вида сжатия",
            ),
            len(data),
        )

    if compression == 0:
        for row_number in range(rows):
            _require(data, index, bytes_per_row, f"оборвана строка растра {row_number}")
            decoded = data[index : index + bytes_per_row]
            consumed = bytes_per_row
            decoded_rows.append(decoded)
            encoded_lengths.append(consumed)
            index += consumed
    else:
        # One native call handles the row-boundary state machine for the whole
        # block.  Structural ESC/P2 parsing intentionally remains independent
        # and readable Python code.
        try:
            native_rows, native_lengths, consumed = packbits_decode_rows(
                data, bytes_per_row, rows, start=index
            )
        except ValueError as error:
            raise ParseError(f"ошибка PackBits по смещению 0x{index:X}: {error}") from error
        decoded_rows.extend(native_rows)
        encoded_lengths.extend(native_lengths)
        index += consumed

    raster = RasterData(
        selector=selector,
        compression=compression,
        bit_depth=bit_depth,
        bytes_per_row=bytes_per_row,
        rows=rows,
        compressed_payload=data[payload_start:index],
        decoded_rows=tuple(decoded_rows),
        encoded_row_lengths=tuple(encoded_lengths),
    )
    params: dict[str, ParameterValue] = {
        "selector": selector,
        "compression": compression,
        "bit_depth": bit_depth,
        "bytes_per_row": bytes_per_row,
        "rows": rows,
        "decoded_length": bytes_per_row * rows,
    }
    return (
        _record(
            data,
            start,
            index,
            "ESC/P2_RASTER",
            "RASTER_IMAGE",
            params,
            raster=raster,
        ),
        index,
    )


def parse_stream(data: bytes) -> list[StreamRecord]:
    """Разобрать весь поток без привязки к имени файла или его хешу."""

    records: list[StreamRecord] = []
    index = 0
    remote_mode = False
    while index < len(data):
        start = index

        if data.startswith(b"\x00\x00\x00\x1b\x01@EJL", index):
            first_lf = data.find(b"\n", index)
            second_lf = data.find(b"\n", first_lf + 1) if first_lf >= 0 else -1
            if second_lf < 0:
                raise ParseError(f"оборван заголовок @EJL по смещению 0x{index:X}")
            index = second_lf + 1
            records.append(
                _record(
                    data,
                    start,
                    index,
                    "EJL",
                    "PACKET_MODE_EXIT",
                    {"text": data[start:index].decode("ascii", errors="replace").rstrip("\n")},
                )
            )
        elif data.startswith(b"\x1b@", index):
            index += 2
            records.append(_record(data, start, index, "ESC/P2", "RESET"))
        elif remote_mode and data.startswith(b"\x1b\x00\x00\x00", index):
            index += 4
            remote_mode = False
            records.append(_record(data, start, index, "REMOTE1", "REMOTE_MODE_EXIT"))
        elif remote_mode:
            _require(data, index, 4, "оборван заголовок REMOTE1")
            code = data[index : index + 2]
            length = unpack_from("<H", data, index + 2)[0]
            end = index + 4 + length
            _require(data, index, 4 + length, "оборвана команда REMOTE1")
            payload = data[index + 4 : end]
            known = code in _REMOTE_NAMES
            records.append(
                _record(
                    data,
                    start,
                    end,
                    "REMOTE1",
                    _REMOTE_NAMES.get(code, f"REMOTE_{code.hex().upper()}"),
                    _decode_remote_parameters(code, payload),
                    EvidenceClass.FACT if known else EvidenceClass.UNKNOWN,
                    (
                        "структура команды подтверждена; смысл сверяется с исходниками Gutenprint"
                        if known
                        else "структура сохранена, семантика идентификатора не установлена"
                    ),
                )
            )
            index = end
        elif data.startswith(b"\x1b(", index):
            _require(data, index, 5, "оборван заголовок расширенной команды")
            code = data[index + 2 : index + 3]
            length = unpack_from("<H", data, index + 3)[0]
            end = index + 5 + length
            _require(data, index, 5 + length, "оборвана расширенная команда")
            payload = data[index + 5 : end]
            is_remote_enter = code == b"R" and payload == b"\x00REMOTE1"
            known = code in _EXTENDED_NAMES or is_remote_enter
            records.append(
                _record(
                    data,
                    start,
                    end,
                    "REMOTE1" if is_remote_enter else "ESC/P2_EXTENDED",
                    (
                        "REMOTE_MODE_ENTER"
                        if is_remote_enter
                        else _EXTENDED_NAMES.get(code, f"ESC_({code.hex().upper()})")
                    ),
                    _decode_extended_parameters(code, payload),
                    EvidenceClass.FACT if known else EvidenceClass.UNKNOWN,
                    (
                        "структура команды подтверждена; смысл сверяется с исходниками Gutenprint"
                        if known
                        else "границы известны по префиксу длины, семантика кода не установлена"
                    ),
                )
            )
            remote_mode = is_remote_enter
            index = end
        elif data.startswith(b"\x1bU", index):
            _require(data, index, 3, "оборвана команда направления")
            index += 3
            records.append(
                _record(
                    data,
                    start,
                    index,
                    "ESC/P2",
                    "DIRECTION",
                    {"unidirectional": bool(data[start + 2]), "value": data[start + 2]},
                )
            )
        elif data.startswith(b"\x1bi", index):
            raster_record, index = _parse_raster(data, start)
            records.append(raster_record)
        elif data[index] == 0x0D:
            index += 1
            records.append(_record(data, start, index, "CONTROL", "CARRIAGE_RETURN"))
        elif data[index] == 0x0C:
            index += 1
            records.append(_record(data, start, index, "CONTROL", "FORM_FEED"))
        elif data[index] == 0x0A:
            index += 1
            records.append(_record(data, start, index, "CONTROL", "LINE_FEED"))
        else:
            index += 1
            while index < len(data) and data[index] not in {0x0A, 0x0C, 0x0D, 0x1B}:
                index += 1
            records.append(
                _record(
                    data,
                    start,
                    index,
                    "UNKNOWN",
                    "UNKNOWN",
                    {"raw_hex": data[start:index].hex(" ")},
                    EvidenceClass.UNKNOWN,
                    "байты сохранены без предположения об их назначении",
                )
            )
    return records


def _format_parameters(parameters: dict[str, ParameterValue]) -> str:
    return " ".join(
        f"{key}={value!r}" if isinstance(value, str) else f"{key}={value}"
        for key, value in parameters.items()
    )


def format_stream(records: list[StreamRecord]) -> str:
    """Сформировать компактный русскоязычный листинг разбора."""

    lines = ["СМЕЩЕНИЕ  КОНЕЦ     КЛАСС   СЕМЕЙСТВО         КОМАНДА                  ПАРАМЕТРЫ"]
    for item in records:
        lines.append(
            f"0x{item.offset:06X} 0x{item.end_offset:06X} {item.confidence:<7} "
            f"{item.family:<17} {item.name:<24} {_format_parameters(item.parameters)}"
        )
    return "\n".join(lines) + "\n"
