from __future__ import annotations

import math
from collections.abc import Iterable
from pathlib import Path

import numpy as np
import rasterio
from pyproj import Transformer


class TerrainService:
    """Load a terrain GeoTIFF and query elevations by latitude/longitude.

    Parameters
    ----------
    geotiff_path:
        Projected, single-band elevation GeoTIFF 
    """

    def __init__(self, geotiff_path: str | Path):
        self.path = Path(geotiff_path)
        self._dataset = rasterio.open(self.path)

        if self._dataset.count != 1:
            self._dataset.close()
            raise ValueError("Terrain GeoTIFF must contain exactly one band.")
        if self._dataset.crs is None:
            self._dataset.close()
            raise ValueError("Terrain GeoTIFF has no coordinate reference system.")

        self._elevations = self._dataset.read(1, masked=True)
        self._to_terrain_crs = Transformer.from_crs(
            "EPSG:4326",
            self._dataset.crs,
            always_xy=True,
        )

    @property
    def crs(self) -> str:
        """Projected CRS used internally by the elevation grid."""

        return self._dataset.crs.to_string()

    @property
    def resolution_m(self) -> tuple[float, float]:
        """Return absolute raster resolution as ``(x_metres, y_metres)``."""

        return (
            float(abs(self._dataset.transform.a)),
            float(abs(self._dataset.transform.e)),
        )

    @property
    def shape(self) -> tuple[int, int]:
        """Return the elevation grid shape as ``(rows, columns)``."""

        return self._dataset.height, self._dataset.width

    def get_elevation(
        self,
        latitude: float,
        longitude: float,
    ) -> float | None:
        """Return bilinearly interpolated elevation in metres.

        ``None`` is returned when the coordinate lies outside the raster, outside
        the original polygon, in a polygon hole, or next to a NoData boundary
        where four valid samples are unavailable.
        """

        projected_x, projected_y = self._to_terrain_crs.transform(
            longitude,
            latitude,
        )
        return self.get_elevation_projected(projected_x, projected_y)

    def get_elevation_projected(
        self,
        projected_x: float,
        projected_y: float,
    ) -> float | None:
        """Query elevation using coordinates already in crs"""

        pixel_column, pixel_row = (~self._dataset.transform) * (
            projected_x,
            projected_y,
        )
        #Para hacerlo relativo al centro del pixel
        sample_column = float(pixel_column) - 0.5
        sample_row = float(pixel_row) - 0.5

        column_0 = math.floor(sample_column)
        row_0 = math.floor(sample_row)
        column_1 = column_0 + 1
        row_1 = row_0 + 1

        if (
            row_0 < 0
            or column_0 < 0
            or row_1 >= self._dataset.height
            or column_1 >= self._dataset.width
        ):
            return None

        window = self._elevations[
            row_0 : row_1 + 1,
            column_0 : column_1 + 1,
        ]
        if np.any(np.ma.getmaskarray(window)):
            return None

        fraction_x = sample_column - column_0
        fraction_y = sample_row - row_0

        top = (
            float(window[0, 0]) * (1.0 - fraction_x)
            + float(window[0, 1]) * fraction_x
        )
        bottom = (
            float(window[1, 0]) * (1.0 - fraction_x)
            + float(window[1, 1]) * fraction_x
        )
        return top * (1.0 - fraction_y) + bottom * fraction_y

    def get_elevations(
        self,
        coordinates: Iterable[tuple[float, float]],
    ) -> list[float | None]:
        """Query several ``(latitude, longitude)`` coordinates in input order."""

        return [
            self.get_elevation(latitude, longitude)
            for latitude, longitude in coordinates
        ]

    def close(self) -> None:
        """Release the open Rasterio dataset."""

        self._dataset.close()

    def __enter__(self) -> "TerrainService":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()
