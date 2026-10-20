# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Row spans for sparse pages, after QTR curves have determined ink demand."""

from collections.abc import Sequence

# Process three empty rows after content, then clear diffusion state on the
# fourth. This is an explicit blank-region boundary policy, not a change to
# diffusion coefficients. Counts persist across bands and are per logical ink.
EMPTY_SETTLE_ROWS = 3


def blank_row_spans(
    active: Sequence[int],
    empty_rows: int,
    *,
    settle_rows: int = 0,
) -> tuple[list[tuple[int, int, bool]], int]:
    """Return (start, end, render) spans without changing absolute row indices.

    For error diffusion the first few empty rows still render incoming error.
    A skipped span is a state-reset boundary. Stateless screens use zero
    settling rows and retain their absolute matrix phase on the next span.
    """
    spans: list[tuple[int, int, bool]] = []
    start = 0
    previous = None
    for index, value in enumerate(active):
        if value:
            empty_rows = 0
            render = True
        else:
            render = empty_rows < settle_rows
            empty_rows = min(settle_rows, empty_rows + 1)
        if previous is not None and render != previous:
            spans.append((start, index, previous))
            start = index
        previous = render
    if previous is not None:
        spans.append((start, len(active), previous))
    return spans, empty_rows
