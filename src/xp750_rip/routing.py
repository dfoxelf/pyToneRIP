# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Generic explicit logical-channel to PhysicalPath routing boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from xp750_rip.contone import LogicalChannelId
from xp750_rip.halftone import LogicalDotPlane
from xp750_rip.raster import NamedRasterPlane, PhysicalPathId, PhysicalRasterSet

if TYPE_CHECKING:
    from xp750_rip.performance import PerformanceTrace


@dataclass(frozen=True, slots=True)
class ChannelRouter:
    """Route arbitrary identified logical planes without positional semantics."""

    mapping: dict[LogicalChannelId, PhysicalPathId]
    allow_multiple_logical_to_one_path: bool = False

    def route(
        self,
        planes: tuple[LogicalDotPlane, ...],
        *,
        available_paths: tuple[PhysicalPathId, ...],
        trace: PerformanceTrace | None = None,
    ) -> PhysicalRasterSet:
        if not planes:
            raise ValueError("ChannelRouter не получил активных logical planes")
        identities = tuple(plane.identity for plane in planes)
        if len(identities) != len(set(identities)):
            raise ValueError("Logical dot plane identities должны быть уникальными")
        missing = tuple(identity for identity in identities if identity not in self.mapping)
        if missing:
            raise ValueError(
                "Нет явного routing для logical channels: "
                + ", ".join(item.value for item in missing)
            )
        targets = tuple(self.mapping[identity] for identity in identities)
        unknown = set(targets) - set(available_paths)
        if unknown:
            raise ValueError(
                "Routing ссылается на отсутствующие PhysicalPaths: "
                + ", ".join(sorted(item.value for item in unknown))
            )
        if not self.allow_multiple_logical_to_one_path and len(targets) != len(set(targets)):
            raise ValueError(
                "Несколько logical channels направлены в один PhysicalPath; mixing не включён"
            )
        if self.allow_multiple_logical_to_one_path and len(targets) != len(set(targets)):
            raise NotImplementedError(
                "Смешивание нескольких halftone planes в одном PhysicalPath ещё не реализовано"
            )
        def create() -> PhysicalRasterSet:
            return PhysicalRasterSet(
                tuple(
                    NamedRasterPlane(
                        identity=target,
                        width_pixels=plane.width_pixels,
                        bit_plane_rows=plane.bit_plane_rows,
                    )
                    for plane, target in zip(planes, targets, strict=True)
                )
            )

        if trace is None:
            return create()
        with trace.stage("PhysicalRasterSet creation", plane_count=len(planes)):
            return create()


@dataclass(frozen=True, slots=True)
class OneToOneChannelRoute:
    """Explicitly route exactly one logical dot plane to one physical path."""

    logical_channel: LogicalChannelId
    physical_path: PhysicalPathId

    def route(self, plane: LogicalDotPlane) -> NamedRasterPlane:
        if plane.identity != self.logical_channel:
            raise ValueError(
                f"route expects logical channel {self.logical_channel}, got {plane.identity}"
            )
        return NamedRasterPlane(
            identity=self.physical_path,
            width_pixels=plane.width_pixels,
            bit_plane_rows=plane.bit_plane_rows,
        )
