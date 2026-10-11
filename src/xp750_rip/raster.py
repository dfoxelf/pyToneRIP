# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Generic immutable N-channel raster containers.

The objects in this module deliberately know nothing about Epson selectors,
factory ink names, installed ink chemistry, or printer-specific scheduling.
"""

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

import numpy as np


@dataclass(frozen=True, slots=True, order=True)
class PhysicalPathId:
    """Stable identity of one printer-addressable physical raster path."""

    value: str

    def __post_init__(self) -> None:
        if not self.value or self.value.strip() != self.value:
            raise ValueError("physical path identity must be a non-empty trimmed string")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class NamedRasterPlane:
    """Packed sample bits for one explicitly identified physical path.

    ``bit_plane_rows[0]`` is the least-significant sample bit. Each row is
    byte-packed, most-significant pixel first. Printer protocol addresses do
    not belong in this generic representation.
    """

    identity: PhysicalPathId
    width_pixels: int
    bit_plane_rows: tuple[tuple[bytes, ...], ...]

    def __post_init__(self) -> None:
        if self.width_pixels <= 0 or self.width_pixels % 8:
            raise ValueError("raster width must be a positive multiple of 8 pixels")
        if not self.bit_plane_rows:
            raise ValueError("raster plane must contain at least one sample bit plane")
        height = len(self.bit_plane_rows[0])
        if height <= 0:
            raise ValueError("raster plane height must be positive")
        row_bytes = self.width_pixels // 8
        for bit_plane in self.bit_plane_rows:
            if len(bit_plane) != height:
                raise ValueError("all sample bit planes must have the same height")
            if any(len(row) != row_bytes for row in bit_plane):
                raise ValueError("every packed row must match the declared raster width")

    @property
    def height_rows(self) -> int:
        return len(self.bit_plane_rows[0])

    @property
    def bit_depth(self) -> int:
        return len(self.bit_plane_rows)

    @property
    def decoded_rows(self) -> tuple[bytes, ...]:
        """Return printer-neutral rows with sample bit planes concatenated."""

        return tuple(
            b"".join(bit_plane[row_index] for bit_plane in self.bit_plane_rows)
            for row_index in range(self.height_rows)
        )

    @property
    def active_bits(self) -> tuple[int, ...]:
        return tuple(
            sum(byte.bit_count() for row in bit_plane for byte in row)
            for bit_plane in self.bit_plane_rows
        )


@dataclass(frozen=True, slots=True)
class PhysicalRasterSet:
    """N physical raster planes with explicit identities and common geometry."""

    planes: tuple[NamedRasterPlane, ...]

    def __post_init__(self) -> None:
        if not self.planes:
            raise ValueError("physical raster set must contain at least one plane")
        identities = tuple(plane.identity for plane in self.planes)
        if len(set(identities)) != len(identities):
            raise ValueError("physical raster identities must be unique")
        reference = self.planes[0]
        for plane in self.planes[1:]:
            if (
                plane.width_pixels,
                plane.height_rows,
                plane.bit_depth,
            ) != (
                reference.width_pixels,
                reference.height_rows,
                reference.bit_depth,
            ):
                raise ValueError("all physical raster planes must share geometry and bit depth")

    @property
    def width_pixels(self) -> int:
        return self.planes[0].width_pixels

    @property
    def height_rows(self) -> int:
        return self.planes[0].height_rows

    @property
    def bit_depth(self) -> int:
        return self.planes[0].bit_depth

    @property
    def identities(self) -> tuple[PhysicalPathId, ...]:
        return tuple(plane.identity for plane in self.planes)

    def get(self, identity: PhysicalPathId) -> NamedRasterPlane:
        for plane in self.planes:
            if plane.identity == identity:
                return plane
        raise KeyError(identity)


@dataclass(frozen=True, slots=True)
class RasterBandPlane:
    """Packed sample bits for one path and one bounded-height band.

    ``bit_planes`` has shape ``(bit_depth, band_height, width_pixels / 8)``.
    Keeping the native NumPy buffer avoids constructing one Python ``bytes``
    object for every row before the ESC/P2 weave consumes the band.
    """

    identity: PhysicalPathId
    bit_planes: np.ndarray

    def __post_init__(self) -> None:
        if self.bit_planes.dtype != np.uint8 or self.bit_planes.ndim != 3:
            raise ValueError("raster band bit planes must be a 3-D uint8 array")
        if not self.bit_planes.flags.c_contiguous:
            raise ValueError("raster band bit planes must be C-contiguous")
        if min(self.bit_planes.shape) <= 0:
            raise ValueError("raster band dimensions must be positive")


@dataclass(frozen=True, slots=True)
class PhysicalRasterBand:
    """One ordered, non-overlapping slice of every active physical path."""

    start_row: int
    planes: tuple[RasterBandPlane, ...]

    def __post_init__(self) -> None:
        if self.start_row < 0 or not self.planes:
            raise ValueError("physical raster band must have a non-negative start and planes")
        shapes = {plane.bit_planes.shape for plane in self.planes}
        identities = tuple(plane.identity for plane in self.planes)
        if len(shapes) != 1:
            raise ValueError("all planes in a raster band must share geometry")
        if len(set(identities)) != len(identities):
            raise ValueError("physical raster band identities must be unique")

    @property
    def height_rows(self) -> int:
        return int(self.planes[0].bit_planes.shape[1])


@dataclass(frozen=True, slots=True)
class StreamingPhysicalRasterSet:
    """Restartable producer of bounded physical raster bands.

    This is the production boundary between QTR/dither and ESC/P2.  Geometry
    is known before rendering, while page-height-dependent pixel buffers are
    created only inside ``iter_bands`` and released after the weave consumes
    them.
    """

    width_pixels: int
    height_rows: int
    bit_depth: int
    identities: tuple[PhysicalPathId, ...]
    band_factory: Callable[[], Iterator[PhysicalRasterBand]] = field(
        repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if self.width_pixels <= 0 or self.width_pixels % 8:
            raise ValueError("streaming raster width must be a positive multiple of 8")
        if self.height_rows <= 0 or self.bit_depth <= 0:
            raise ValueError("streaming raster height and bit depth must be positive")
        if not self.identities or len(set(self.identities)) != len(self.identities):
            raise ValueError("streaming raster identities must be non-empty and unique")

    def iter_bands(self) -> Iterator[PhysicalRasterBand]:
        expected_start = 0
        for band in self.band_factory():
            if band.start_row != expected_start:
                raise ValueError("streaming raster bands must be contiguous and ordered")
            if tuple(plane.identity for plane in band.planes) != self.identities:
                raise ValueError("streaming raster band path order changed")
            shape = band.planes[0].bit_planes.shape
            if shape[0] != self.bit_depth or shape[2] != self.width_pixels // 8:
                raise ValueError("streaming raster band geometry changed")
            expected_start += band.height_rows
            if expected_start > self.height_rows:
                raise ValueError("streaming raster bands exceed declared height")
            yield band
        if expected_start != self.height_rows:
            raise ValueError("streaming raster ended before its declared height")
