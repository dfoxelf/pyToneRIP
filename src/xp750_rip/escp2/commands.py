# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Encoding for the Epson ESC/P2 commands used by native printer backends.

Sources: Gutenprint 5.3.5 ``src/main/escp2-driver.c`` lines 48-56,
221-337, 339-496, 525-583, 644-681 and 791-802; model data comes from
``src/xml/escp2/model/model_96.xml``.
"""

from struct import pack

from xp750_rip.escp2.channels import Escp2InkAddress

ESC = b"\x1b"


def _extended(command: bytes, payload: bytes) -> bytes:
    if len(command) != 1:
        raise ValueError("extended command must be one byte")
    return ESC + b"(" + command + pack("<H", len(payload)) + payload


def _remote(command: bytes, payload: bytes) -> bytes:
    if len(command) != 2:
        raise ValueError("REMOTE1 command must be two bytes")
    return command + pack("<H", len(payload)) + payload


def extended_payload(command: str, payload: bytes) -> bytes:
    """Encode a validated raw ESC (...) payload stored in a model profile."""

    encoded = command.encode("ascii")
    return _extended(encoded, payload)


def remote_payload(command: str, payload: bytes) -> bytes:
    """Encode one data-described REMOTE1 command.

    Only two-letter commands accepted by the importer can reach this function;
    length validation is repeated here as a protocol boundary guard.
    """

    encoded = command.encode("ascii")
    return _remote(encoded, payload)


def _u8(value: int, label: str) -> int:
    if not 0 <= value <= 0xFF:
        raise ValueError(f"{label} must fit an unsigned byte")
    return value


def _u32(value: int, label: str) -> int:
    if not 0 <= value <= 0xFFFFFFFF:
        raise ValueError(f"{label} must fit an unsigned 32-bit integer")
    return value


def ejl_packet_mode_exit(protocol: str = "1284.4") -> bytes:
    """Build the model-96 EJL sequence that exits IEEE-1284.4 packet mode."""
    encoded_protocol = protocol.encode("ascii")
    return b"\x00\x00\x00\x1b\x01@EJL " + encoded_protocol + b"\n@EJL     \n"


def reset() -> bytes:
    return ESC + b"@"


def remote_mode_enter() -> bytes:
    return _extended(b"R", b"\x00REMOTE1")


def remote_mode_exit() -> bytes:
    return ESC + b"\x00\x00\x00"


def remote_job_start(label: str) -> bytes:
    encoded_label = label.encode("ascii")
    if b"\x00" in encoded_label:
        raise ValueError("REMOTE1 job label must not contain NUL")
    return _remote(b"JS", b"\x00" + encoded_label + b"\x00")


def remote_job_start_extended(
    *, flags: bytes, metadata: tuple[bytes, bytes, bytes, bytes, bytes],
) -> bytes:
    """Encode the verified ReferenceDriver JX start dialect as an explicit opt-in.

    Flags and metadata remain byte-valued: their application-level meanings
    and text encoding are model dependent. Metadata follows tags 08/0C/0D/18/17.
    Reject oversized fields instead of truncating the one-byte lengths.
    """

    if not isinstance(flags, bytes) or len(flags) != 4:
        raise ValueError("JX flags must contain exactly four bytes")
    if len(metadata) != 5:
        raise ValueError("JX metadata must contain exactly five fields")
    payload = b"\x00\x00\x01\x01\x01\x02\x08" + flags + bytes(4) + b"\x0f\x02\x01\x00"
    for tag, value in zip((0x08, 0x0C, 0x0D, 0x18, 0x17), metadata, strict=True):
        if not isinstance(value, bytes) or len(value) > 255:
            raise ValueError("each JX metadata field must contain at most 255 bytes")
        payload += bytes((tag, len(value))) + value
    return _remote(b"JX", payload)


def remote_job_end_extended() -> bytes:
    """Encode the matching verified JX end record, without assuming EX lifecycle."""

    return _remote(b"JX", b"\x00\x00\x01\x01\x02")


def remote_weave_resource(resource: bytes) -> bytes:
    """Wrap an externally supplied opaque resource in EX selector 0037.

    Resource provenance, model identity and placement in the job must be
    checked by the caller. No third-party resource is bundled by this codec.
    """

    if not isinstance(resource, bytes) or not 0 < len(resource) <= 65529:
        raise ValueError("EX resource must contain 1..65529 bytes")
    return _remote(b"EX", bytes(4) + b"\x37\x00" + resource)


def remote_sn(value: int = 0) -> bytes:
    return _remote(b"SN", bytes((_u8(value, "SN value"),)))


def remote_paper_path(*, path_type: int, path_number: int) -> bytes:
    return _remote(
        b"PP",
        bytes((0, _u8(path_type, "path_type"), _u8(path_number, "path_number"))),
    )


def remote_media_info(*, media_format: int, media_type: int, media_size: int) -> bytes:
    return _remote(
        b"MI",
        bytes(
            (
                0,
                _u8(media_format, "media_format"),
                _u8(media_type, "media_type"),
                _u8(media_size, "media_size"),
            )
        ),
    )


def remote_feed_adjustment(value: int) -> bytes:
    """Encode paper-feed correction in Epson's documented 0.01% units."""

    return _remote(b"SN", bytes((0, 4, _u8(value, "feed adjustment"))))


def remote_vacuum_intensity(value: int) -> bytes:
    """Encode the printer-specific vacuum/suction byte (SN selector 5)."""

    return _remote(b"SN", bytes((0, 5, _u8(value, "vacuum intensity"))))


def remote_paper_thickness(value: int, *, payload_length: int = 2) -> bytes:
    """Encode the model's PH byte or word; the wire units are model specific."""

    if payload_length == 2:
        payload = bytes((0, _u8(value, "paper thickness")))
    elif payload_length == 3:
        if not 0 <= value <= 0xFFFF:
            raise ValueError("paper thickness must fit an unsigned 16-bit integer")
        payload = b"\x00" + pack("<H", value)
    else:
        raise ValueError("paper thickness payload_length must be 2 or 3")
    return _remote(b"PH", payload)


def remote_feed_sequence(value: int) -> bytes:
    """Select a printer-specific paper-feed sequence (SN selector 0)."""

    return _remote(b"SN", bytes((0, 0, _u8(value, "feed sequence"))))


def remote_platen_gap(value: int) -> bytes:
    """Select the printer-specific platen/head gap (REMOTE1 US selector 1)."""

    return _remote(b"US", bytes((0, 1, _u8(value, "platen gap"))))


def remote_roll_cut(*, mode: str, page_height: int) -> bytes:
    """Schedule an Epson roll cut at a page-relative position.

    The protocol uses two CO records. The first enables page cutting; the
    second selects cutting every page (0) or only the final page (2) and
    carries the page height as LE32. These are
    mechanical commands and are unrelated to color processing.

    Протокол использует две записи CO: первая включает планирование реза,
    вторая задаёт рез каждой страницы (0) либо только последней (2) и
    содержит высоту страницы LE32.
    """

    operation_by_mode = {"cut_each_page": 0, "cut_last_page": 2}
    try:
        operation = operation_by_mode[mode]
    except KeyError as error:
        raise ValueError(f"Unsupported roll cut mode: {mode}") from error
    checked_height = _u32(page_height, "roll cut page_height")
    enable = _remote(b"CO", bytes((0, 0, 1, 0)) + pack("<L", 0))
    position = _remote(
        b"CO", bytes((0, 0, operation, 0)) + pack("<L", checked_height)
    )
    return enable + position


def remote_cutter_edge_trim(mode: str) -> bytes:
    """Select standard, single-edge or double-edge borderless roll trimming."""

    value_by_mode = {"standard": 0, "single_edge": 1, "double_edge": 2}
    try:
        value = value_by_mode[mode]
    except KeyError as error:
        raise ValueError(f"Unsupported cutter edge trim mode: {mode}") from error
    # SN selector 0x84 belongs to roll-cutter mechanics. It is distinct from
    # the CO page-cut plan and from media/color selection.
    # SN 0x84 задаёт обработку краёв рулона отдельно от плана резки CO.
    return _remote(b"SN", bytes((0, 0x84, value)))


def remote_load_defaults() -> bytes:
    return _remote(b"LD", b"")


def remote_job_end(value: int = 0) -> bytes:
    return _remote(b"JE", bytes((_u8(value, "JE value"),)))


def remote_head_clean() -> bytes:
    """Run one standard cleaning cycle (REMOTE1 ``CH 02 00 00 00``).

    The two zero bytes and command framing match Gutenprint escputil's
    ``do_head_clean`` implementation. / Два нулевых байта и framing команды
    совпадают с открытой реализацией обслуживания Epson в Gutenprint.
    """

    return _remote(b"CH", b"\x00\x00")


def remote_nozzle_check() -> bytes:
    """Request the printer's native nozzle-check page.

    Epson firmware expects VI first and then two NC records. This is a device
    generated diagnostic page; no RIP raster follows these commands.
    """

    return b"".join(
        (
            _remote(b"VI", b"\x00\x00"),
            _remote(b"NC", b"\x00\x10"),
            _remote(b"NC", b"\x00\x00"),
        )
    )


def graphics_mode() -> bytes:
    return _extended(b"G", b"\x01")


def units(*, page: int, vertical: int, horizontal: int, scale: int) -> bytes:
    if not all(scale % value == 0 for value in (page, vertical, horizontal)):
        raise ValueError("unit scale must be divisible by each resolution")
    return _extended(
        b"U",
        bytes((scale // page, scale // vertical, scale // horizontal)) + pack("<H", scale),
    )


def gray_mode(mode: int = 1) -> bytes:
    """Encode ESC (K; model 96 uses 1 for black parameters and 2 for color."""
    return _extended(b"K", bytes((0, mode)))


def microweave(mode: int = 0, *, variant: int | None = None) -> bytes:
    """Encode the model-selected one- or three-byte firmware weave form.

    The extended form is ``mode, variant, 0``. Its second byte is model
    specific; it must not be inferred from the print-method number.
    """

    payload = bytes((_u8(mode, "microweave mode"),))
    if variant is not None:
        payload += bytes((_u8(variant, "microweave variant"), 0))
    return _extended(b"i", payload)


def unidirectional(enabled: bool) -> bytes:
    return ESC + b"U" + bytes((int(enabled),))


def dot_size(size: int) -> bytes:
    return _extended(b"e", bytes((0, _u8(size, "dot size"))))


def printhead_resolution(*, scale: int, vertical: int, horizontal: int) -> bytes:
    """Build ESC (D; its scale is distinct from the ESC (U unit scale."""
    if not 0 < scale <= 0xFFFF:
        raise ValueError("printhead resolution scale must fit an unsigned 16-bit value")
    return _extended(
        b"D",
        pack("<H", scale)
        + bytes(
            (
                _u8(vertical, "printhead vertical unit"),
                _u8(horizontal, "printhead horizontal unit"),
            )
        ),
    )


def page_length(units_value: int) -> bytes:
    return _extended(b"C", pack("<I", units_value))


def page_format(top: int, bottom: int) -> bytes:
    return _extended(b"c", pack("<ii", top, bottom))


def paper_dimensions(width: int, height: int) -> bytes:
    return _extended(b"S", pack("<II", width, height))


def print_method(method: int, *, payload_length: int = 1) -> bytes:
    """Encode a model-qualified ESC (m dialect, preserving its exact width.

    The four-byte form contains a little-endian method word followed by two
    reserved zero bytes. It is distinct from the legacy one-byte form even
    when the method number is the same.
    """

    if payload_length == 1:
        payload = bytes((_u8(method, "print method"),))
    elif payload_length == 4:
        if not 0 <= method <= 0xFFFF:
            raise ValueError("print method must fit an unsigned 16-bit integer")
        payload = pack("<HH", method, 0)
    else:
        raise ValueError("print method payload_length must be 1 or 4")
    return _extended(b"m", payload)


def relative_vertical(advance: int) -> bytes:
    return _extended(b"v", pack("<I", advance))


def absolute_horizontal(position: int) -> bytes:
    return _extended(b"$", pack("<I", position))


def absolute_vertical(position: int) -> bytes:
    return _extended(b"V", pack("<I", position))


def raster_header(
    *, selector: int, compression: int, bit_depth: int, bytes_per_row: int, rows: int
) -> bytes:
    """Encode ``ESC i r c b nL nH mL mH`` (Gutenprint send_print_command)."""
    for value, label in ((bytes_per_row, "bytes_per_row"), (rows, "rows")):
        if not 0 < value <= 0xFFFF:
            raise ValueError(f"{label} must fit an unsigned 16-bit value")
    return ESC + b"i" + bytes((selector, compression, bit_depth)) + pack("<HH", bytes_per_row, rows)


def addressed_raster_header(
    *,
    address: Escp2InkAddress,
    compression: int,
    bit_depth: int,
    bytes_per_row: int,
    rows: int,
) -> bytes:
    """Encode ESC i from separate source-level color and subchannel fields."""
    return raster_header(
        selector=address.selector,
        compression=compression,
        bit_depth=bit_depth,
        bytes_per_row=bytes_per_row,
        rows=rows,
    )


def form_feed() -> bytes:
    return b"\x0c"


def line_feed() -> bytes:
    return b"\x0a"


def carriage_return() -> bytes:
    return b"\x0d"
