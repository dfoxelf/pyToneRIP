# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Generic print-direction policy shared by jobs and printer backends."""

from enum import StrEnum


class PrintDirection(StrEnum):
    BIDIRECTIONAL = "bidirectional"
    UNIDIRECTIONAL = "unidirectional"


def parse_print_direction(value: object) -> PrintDirection:
    """Parse a direction while migrating the old misleading ``auto`` value.

    Earlier releases called the mode's fixed direction policy "auto".  Every
    currently supported mode resolved it to bidirectional, so an existing
    saved configuration can be migrated without changing its print stream.
    """

    normalized = str(value).strip().casefold()
    if normalized == "auto":
        normalized = PrintDirection.BIDIRECTIONAL.value
    return PrintDirection(normalized)
