# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Production TIFF PackBits codec backed only by compiled Cython kernels."""


def encode(data: bytes) -> bytes:
    """Encode one raster row in the ahead-of-time compiled Cython kernel."""

    from xp750_rip._cython_kernels import packbits_encode

    return packbits_encode(data)


def decode_rows(
    data: bytes,
    expected_length: int,
    row_count: int,
    *,
    start: int = 0,
) -> tuple[tuple[bytes, ...], tuple[int, ...], int]:
    """Decode an ESC/P2 raster block in the AOT-compiled Cython kernel.

    The return value contains decoded rows, each encoded row length, and the
    total number of consumed input bytes. Tests compare it with the independent
    oracle in ``tests/packbits_reference.py``; no Python decoder runs in production.
    """

    from xp750_rip._cython_kernels import packbits_decode_rows

    return packbits_decode_rows(data, start, expected_length, row_count)
