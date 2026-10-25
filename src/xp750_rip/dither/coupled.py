# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Adaptive error diffusion with explicit logical C/M and LC/LM coupling.

The recurrence and quantizer construction follow verified ReferenceDriver
behaviour. Physical drop endpoints, widened error state, independent PRNG
streams per group, LC/LM coupling and sparse-page boundaries belong to this
implementation. This is not a complete third-party engine compatibility claim.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from zlib import crc32

import numpy as np

from xp750_rip.contone import UINT16_MAX, LogicalChannelId
from xp750_rip.dither.blank import EMPTY_SETTLE_ROWS, blank_row_spans
from xp750_rip.dither.ordered import GutenprintDropModel
from xp750_rip.halftone import LogicalDotPlane

PAIRED_LOGICAL_CHANNELS = (("C", "M"), ("LC", "LM"))


def coupled_channel_groups(
    inks: Sequence[LogicalChannelId], *, couple_light_inks: bool = True,
) -> tuple[tuple[LogicalChannelId, ...], ...]:
    selected = {ink.value: ink for ink in inks}
    groups = []
    used = set()
    pairs = PAIRED_LOGICAL_CHANNELS if couple_light_inks else PAIRED_LOGICAL_CHANNELS[:1]
    for ink in inks:
        if ink in used:
            continue
        pair = next((pair for pair in pairs
                     if ink.value in pair and all(name in selected for name in pair)), None)
        group = tuple(selected[name] for name in pair) if pair else (ink,)
        groups.append(group)
        used.update(group)
    return tuple(groups)


def adaptive_level_table(drops: GutenprintDropModel) -> np.ndarray:
    """Build the packed level/code, random amplitude and coupling-strength table."""
    table = np.zeros((2048, 1), dtype=np.int64)
    start = previous_level = previous_base = 0
    for endpoint in drops.endpoints[1:]:
        level = endpoint.value * 127 // UINT16_MAX
        if level <= previous_level:
            raise ValueError("Coupled ED needs distinct drop endpoints on its 7-bit level grid")
        end = level + 256
        length = end - start
        base = previous_base
        for index in range(start, end + 1):
            distance = index - start
            if previous_level == 0:
                base = (level - previous_level) * distance // length + 1
                strength = (end - index) * 255 // length
            else:
                base = ((level - previous_level) * distance
                        + (end - index) * previous_base) // length
                strength = 0
            table[index, 0] = endpoint.bits | (level << 8) | (base << 16) | (strength << 24)
        start, previous_level, previous_base = end, level, base
    table[start:, 0] = drops.endpoints[-1].bits | (previous_level << 8)
    return table


@dataclass(frozen=True, slots=True)
class CoupledErrorDiffusion:
    drops: GutenprintDropModel
    x_offset: int = 0
    y_offset: int = 0
    serpentine: bool = True
    random_seed: int = 0x5EED1234
    couple_light_inks: bool = True

    def iter_bands(
        self,
        sources: Mapping[LogicalChannelId, Sequence[bytes]],
        luts: Mapping[str, tuple[int, ...]],
        inks: tuple[LogicalChannelId, ...],
        *,
        canvas_width: int,
        x: int = 0,
        y: int = 0,
        band_rows: int = 256,
        cancel_check: Callable[[], None] | None = None,
        skip_blank_rows: bool = True,
    ):
        """Yield restartable bounded bands; state belongs to this invocation only.

        The input sources may be lazy row sequences. Only a group of one/two
        channels is materialized at once. Blankness is evaluated after LUTs;
        each pair settles and resets together, preserving its serial history.
        """
        from xp750_rip._cython_kernels import adaptive_ed_band_kernel, qtr_active_rows_kernel

        if not inks or len(set(inks)) != len(inks):
            raise ValueError("Coupled ED requires unique selected logical channels")
        height = len(sources[inks[0]])
        width = len(sources[inks[0]][0]) if height else 0
        if (width <= 0 or height <= 0 or band_rows <= 0 or canvas_width <= 0
                or canvas_width % 8 or x < 0 or y < 0 or x + width > canvas_width):
            raise ValueError("invalid Coupled ED canvas or source dimensions")
        for ink in inks:
            if len(sources[ink]) != height:
                raise ValueError("Coupled ED sources must have equal heights")
            lut = luts.get(ink.value)
            if lut is None or len(lut) not in (256, 65536) or any(not 0 <= value <= UINT16_MAX for value in lut):
                raise ValueError(f"invalid QTR LUT for {ink.value}")
        groups = coupled_channel_groups(inks, couple_light_inks=self.couple_light_inks)
        levels = adaptive_level_table(self.drops)
        states = [np.zeros((len(group), width + 2), dtype=np.int64) for group in groups]
        seeds = [(self.random_seed ^ crc32(",".join(ink.value for ink in group).encode("utf-8")))
                 & 0xFFFFFFFF for group in groups]
        empty_runs = [EMPTY_SETTLE_ROWS] * len(groups)
        group_luts = [np.array([luts[ink.value] for ink in group], dtype=np.uint16) for group in groups]
        common_source = sources[inks[0]]
        shared_source = all(sources[ink] is common_source for ink in inks)

        def code_band(source, start, end):
            rows = [source[row] for row in range(start, end)]
            if any(len(row) != width for row in rows):
                raise ValueError("Coupled ED source rows must have equal widths")
            return np.frombuffer(b"".join(rows), dtype=np.uint16 if len(luts[inks[0].value]) == 65536 else np.uint8).reshape(end - start, width)

        for start in range(0, height, band_rows):
            if cancel_check:
                cancel_check()
            end = min(start + band_rows, height)
            packed_by_ink = {}
            # A grayscale QTR job shares one source across all curves. Decode
            # this bounded band once, including the lazy polarity conversion.
            common_codes = code_band(common_source, start, end) if shared_source else None
            common_zero = common_codes is not None and not np.any(common_codes)
            for number, group in enumerate(groups):
                codes = np.stack([
                    common_codes if common_codes is not None else code_band(sources[ink], start, end)
                    for ink in group
                ])
                active = np.zeros(end - start, dtype=np.uint8)
                if common_zero:
                    # Input zero is blank only when every selected curve also
                    # requests zero. Settling and error reset still run below.
                    active.fill(bool(np.any(group_luts[number][:, 0])))
                else:
                    for channel in range(len(group)):
                        active |= qtr_active_rows_kernel(codes[channel], group_luts[number][channel])
                spans, empty_runs[number] = blank_row_spans(
                    active, empty_runs[number], settle_rows=EMPTY_SETTLE_ROWS,
                )
                packed = np.zeros((len(group), self.drops.bit_depth, end - start,
                                   canvas_width // 8), dtype=np.uint8)
                for first, last, render in spans:
                    if cancel_check:
                        cancel_check()
                    if not render:
                        states[number].fill(0)
                        if skip_blank_rows:
                            continue
                    result, state, seed = adaptive_ed_band_kernel(
                        np.ascontiguousarray(codes[:, first:last]), group_luts[number], levels,
                        states[number], canvas_width, self.drops.bit_depth,
                        y + self.y_offset + start + first, x, self.serpentine,
                        seeds[number], len(group) == 2,
                    )
                    packed[:, :, first:last] = result
                    states[number], seeds[number] = state, seed
                for channel, ink in enumerate(group):
                    packed_by_ink[ink] = np.ascontiguousarray(packed[channel])
            yield start, packed_by_ink

    def dither_channels_placed(
        self, sources, luts, inks, *, canvas_width: int, canvas_height: int, x: int = 0, y: int = 0,
    ) -> tuple[LogicalDotPlane, ...]:
        height = len(sources[inks[0]]) if inks else 0
        if canvas_height <= 0 or y < 0 or y + height > canvas_height:
            raise ValueError("Coupled ED source exceeds canvas height")
        output = {ink: np.zeros((self.drops.bit_depth, canvas_height, canvas_width // 8),
                                dtype=np.uint8) for ink in inks}
        for start, band in self.iter_bands(sources, luts, inks, canvas_width=canvas_width, x=x, y=y):
            for ink, packed in band.items():
                output[ink][:, y + start:y + start + packed.shape[1]] = packed
        return tuple(LogicalDotPlane(ink, canvas_width, tuple(
            tuple(row.tobytes() for row in bit_plane) for bit_plane in output[ink]
        )) for ink in inks)

    def dither_qtr_codes_placed(self, code_rows, qtr_lut, *, identity, **placement):
        return self.dither_channels_placed(
            {identity: code_rows}, {identity.value: qtr_lut}, (identity,), **placement,
        )[0]
