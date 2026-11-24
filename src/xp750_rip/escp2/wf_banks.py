# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Mode-specific row-bank routing around the shared host weave scheduler."""

from dataclasses import replace
from fractions import Fraction
from math import lcm

import numpy as np

from xp750_rip.raster import (
    PhysicalPathId, PhysicalRasterBand, RasterBandPlane, StreamingPhysicalRasterSet,
)
from xp750_rip.escp2.weave import (
    _iter_bitplane_bands, schedule_model96_softweave, WeaveRasterBlock,
)


def schedule_workforce_banks(rasters, mode, *, setup, path_definitions, **kwargs):
    """Partition each ink into its banks without copying an entire page.

    Virtual identities exist only while assigning nozzles. Returned blocks
    retain the original physical ink identity, including in streaming mode.
    """
    if not setup.banks:
        return schedule_model96_softweave(
            rasters, mode, path_definitions=path_definitions, **kwargs)
    definitions = {p.identity: p for p in path_definitions}
    virtual = []
    owners = {}
    routes = []
    nozzle_counts = {}
    physical_counts = dict(setup.channel_nozzle_counts)
    row_scale = lcm(*(Fraction(offset * mode.printed_vertical_dpi,
                              mode.vertical_units).denominator
                      for offset in (
                          *(bank.offset_command_units for bank in setup.banks),
                          *(definitions[p].head_offset for p in rasters.identities))))
    weave_mode = replace(mode, printed_vertical_dpi=mode.printed_vertical_dpi*row_scale,
                         weave_vertical_dpi=(mode.weave_vertical_dpi or mode.physical_vertical_dpi)*row_scale)
    for identity in rasters.identities:
        path = definitions[identity]
        banks = [b for b in setup.banks if b.primary_selector == path.escp2_selector]
        if not banks:
            raise ValueError(f"No native banks for physical ink {identity}")
        for bank in banks:
            bank_id = PhysicalPathId(f"{identity.value}/bank/{bank.selector}")
            owners[bank_id] = identity
            if physical_counts:
                nozzle_counts[bank_id] = physical_counts.get(bank.primary_selector, mode.nozzle_count)
            virtual.append(replace(
                path, identity=bank_id, escp2_selector=bank.selector,
                additional_escp2_selectors=(),
                head_offset=path.head_offset + bank.offset_command_units,
                allocation_index=path.allocation_index * 2 + bank.phase,
            ))
            routes.append((identity, bank_id, bank))

    def bands():
        for start, planes in _iter_bitplane_bands(rasters, 512):
            by_path = dict(planes)
            height = next(iter(by_path.values())).shape[1]
            rows = start + np.arange(height)
            output = []
            for identity, bank_id, bank in routes:
                source = by_path[identity]
                if bank.bank_count == 1:
                    bits = source
                else:
                    bits = source.copy()
                    keep = (rows // bank.row_group) % bank.bank_count == bank.phase
                    bits[:, ~keep, :] = 0
                if row_scale > 1:
                    expanded = np.zeros((bits.shape[0], bits.shape[1]*row_scale, bits.shape[2]), dtype=bits.dtype)
                    expanded[:, ::row_scale, :] = bits
                    bits = expanded
                output.append(RasterBandPlane(bank_id, bits))
            yield PhysicalRasterBand(start*row_scale, tuple(output))

    routed = StreamingPhysicalRasterSet(
        rasters.width_pixels, rasters.height_rows*row_scale, rasters.bit_depth,
        tuple(p.identity for p in virtual), bands,
    )

    def restore(item):
        return replace(item, blocks=tuple(
            WeaveRasterBlock(owners[b.path], replace(b.raster, source_rows=tuple(
                None if row is None or row % row_scale else row//row_scale
                for row in b.raster.source_rows))) for b in item.blocks))

    sink = kwargs.pop("pass_sink", None)
    schedule = schedule_model96_softweave(
        routed, weave_mode, path_definitions=tuple(virtual),
        path_nozzle_counts=nozzle_counts or None,
        pass_sink=(lambda item: sink(restore(item))) if sink is not None else None,
        **{**kwargs, "first_line": kwargs["first_line"]*row_scale,
           "page_height_rows": kwargs["page_height_rows"]*row_scale,
           "initial_feed_offset_rows": kwargs.get("initial_feed_offset_rows", 0)*row_scale},
    )
    return replace(schedule, passes=tuple(restore(p) for p in schedule.passes),
                   rows=tuple((owners[p], row) for p, row in schedule.rows))
