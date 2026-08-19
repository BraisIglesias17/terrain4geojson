"""Generate a metric GLB terrain mesh from a GeoJSON polygon and Mapterhorn.

Pipeline
--------
1. Read a Polygon/MultiPolygon from GeoJSON (geometry, Feature, or FeatureCollection).
2. Find all XYZ tiles intersecting its bounding box.
3. Download Mapterhorn's 512 px Terrarium-encoded WebP elevation tiles.
4. Decode RGB pixels to elevations in metres.
5. Convert every retained pixel centre from the global XYZ/Web-Mercator grid
   into a local UTM coordinate system measured in metres.
6. Retain terrain cells whose centres lie inside the requested polygon.
7. Export the resulting triangle mesh as GLB and write a JSON metadata sidecar.

The output mesh is a terrain surface intended for Three.js, Unity, GIS-style
visualisation, route overlays, and spatial analysis. It is not a closed solid
for 3D printing.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any
import rasterio
import mercantile
import numpy as np
import requests
import trimesh
from PIL import Image
from pyproj import CRS, Transformer
from rasterio.features import geometry_mask
from rasterio.transform import Affine
from rasterio.warp import Resampling, calculate_default_transform, reproject
from shapely.geometry import Point, shape,mapping
from shapely.ops import transform as shapely_transform
from shapely.prepared import prep


MAPTERHORN_TILE_URL = "https://tiles.mapterhorn.com/{z}/{x}/{y}.webp"
TILE_SIZE = 512
WEB_MERCATOR_HALF_WORLD = 20037508.342789244
GEOTIFF_NODATA = -9999.0

@dataclass(frozen=True)
class TileGrid:
    """Rectangular XYZ tile grid covering the polygon's bounding box."""

    zoom: int
    min_x: int
    max_x: int
    min_y: int
    max_y: int

    @property
    def columns(self) -> int:
        return self.max_x - self.min_x + 1

    @property
    def rows(self) -> int:
        return self.max_y - self.min_y + 1

    @property
    def count(self) -> int:
        return self.columns * self.rows

    def tiles(self) -> list[mercantile.Tile]:
        return [
            mercantile.Tile(x=x, y=y, z=self.zoom)
            for y in range(self.min_y, self.max_y + 1)
            for x in range(self.min_x, self.max_x + 1)
        ]


def parse_geojson_geometry(document: dict[str, Any]):
    """Return one Shapely Polygon/MultiPolygon from common GeoJSON forms."""

    object_type = document.get("type")

    if object_type in {"Polygon", "MultiPolygon"}:
        geometry = shape(document)
    elif object_type == "Feature":
        if not document.get("geometry"):
            raise ValueError("The GeoJSON Feature has no geometry.")
        geometry = shape(document["geometry"])
    elif object_type == "FeatureCollection":
        polygons = []
        for feature in document.get("features", []):
            candidate = feature.get("geometry")
            if candidate and candidate.get("type") in {"Polygon", "MultiPolygon"}:
                polygons.append(shape(candidate))
        if not polygons:
            raise ValueError("The FeatureCollection contains no polygon geometry.")
        # Unary union accepts overlapping or disjoint polygon features.
        from shapely.ops import unary_union

        geometry = unary_union(polygons)
    else:
        raise ValueError(
            "Input must be a Polygon, MultiPolygon, Feature, or FeatureCollection."
        )

    if geometry.is_empty:
        raise ValueError("The input polygon is empty.")
    if not geometry.is_valid:
        # buffer(0) repairs many common self-intersection/ring issues.
        geometry = geometry.buffer(0)
    if geometry.is_empty or geometry.geom_type not in {"Polygon", "MultiPolygon"}:
        raise ValueError("The polygon could not be repaired into a valid geometry.")

    min_lon, min_lat, max_lon, max_lat = geometry.bounds
    if not (-180 <= min_lon <= 180 and -180 <= max_lon <= 180):
        raise ValueError("Longitude must lie between -180 and 180 degrees.")
    if not (-85.05112878 <= min_lat <= 85.05112878):
        raise ValueError("Latitude is outside the Web Mercator range.")
    if not (-85.05112878 <= max_lat <= 85.05112878):
        raise ValueError("Latitude is outside the Web Mercator range.")

    return geometry


def choose_local_crs(polygon_wgs84) -> CRS:
    """Choose the WGS84 UTM zone containing the polygon centroid.

    UTM provides metre-based local coordinates. A single UTM zone is appropriate
    for the relatively compact areas that should be converted into one mesh.
    """

    centroid = polygon_wgs84.centroid
    zone = min(60, max(1, int(math.floor((centroid.x + 180.0) / 6.0)) + 1))
    epsg = (32600 if centroid.y >= 0 else 32700) + zone
    return CRS.from_epsg(epsg)


def make_tile_grid(polygon_wgs84, zoom: int) -> TileGrid:
    """Return the rectangular XYZ grid covering the polygon bounds."""

    west, south, east, north = polygon_wgs84.bounds
    tiles = list(mercantile.tiles(west, south, east, north, zooms=[zoom]))
    if not tiles:
        raise ValueError("No XYZ tiles intersect the supplied polygon.")

    return TileGrid(
        zoom=zoom,
        min_x=min(tile.x for tile in tiles),
        max_x=max(tile.x for tile in tiles),
        min_y=min(tile.y for tile in tiles),
        max_y=max(tile.y for tile in tiles),
    )


def decode_terrarium(webp_bytes: bytes) -> np.ndarray:
    """Decode one Terrarium RGB image into a float32 elevation matrix.

    Terrarium formula: elevation = R*256 + G + B/256 - 32768 metres.
    """

    with Image.open(BytesIO(webp_bytes)) as image:
        rgb = np.asarray(image.convert("RGB"), dtype=np.float32)

    if rgb.shape != (TILE_SIZE, TILE_SIZE, 3):
        raise ValueError(f"Unexpected Mapterhorn tile shape: {rgb.shape}")

    return rgb[..., 0] * 256.0 + rgb[..., 1] + rgb[..., 2] / 256.0 - 32768.0


def download_tile(tile: mercantile.Tile, timeout: float) -> np.ndarray:
    """Download and decode one Mapterhorn tile."""

    url = MAPTERHORN_TILE_URL.format(z=tile.z, x=tile.x, y=tile.y)
    response = requests.get(
        url,
        timeout=timeout,
        headers={"User-Agent": "mapterhorn-terrain-generator/1.0"},
    )
    response.raise_for_status()
    return decode_terrarium(response.content)


def download_mosaic(
    grid: TileGrid,
    workers: int,
    timeout: float,
) -> np.ndarray:
    """Download a tile grid concurrently and stitch it north-to-south."""

    mosaic = np.empty(
        (grid.rows * TILE_SIZE, grid.columns * TILE_SIZE),
        dtype=np.float32,
    )

    def place(tile: mercantile.Tile, elevations: np.ndarray) -> None:
        row = tile.y - grid.min_y
        column = tile.x - grid.min_x
        row_start = row * TILE_SIZE
        column_start = column * TILE_SIZE
        mosaic[
            row_start : row_start + TILE_SIZE,
            column_start : column_start + TILE_SIZE,
        ] = elevations

    tiles = grid.tiles()
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(download_tile, tile, timeout): tile for tile in tiles}
        completed = 0
        for future in as_completed(futures):
            tile = futures[future]
            try:
                place(tile, future.result())
            except Exception as exc:
                # Cancel pending requests and report the exact failed tile.
                for pending in futures:
                    pending.cancel()
                raise RuntimeError(
                    f"Could not download tile z={tile.z}, x={tile.x}, y={tile.y}: {exc}"
                ) from exc
            completed += 1
            print(f"Downloaded {completed}/{grid.count} tiles", file=sys.stderr)

    return mosaic


def global_pixel_centres_web_mercator(
    grid: TileGrid,
    sampled_rows: np.ndarray,
    sampled_columns: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Convert mosaic pixel centres to exact EPSG:3857 coordinates.

    XYZ tiles form a global square of TILE_SIZE * 2**zoom pixels. Using global
    pixel indices avoids the incorrect assumption that latitude is linear.
    """

    world_pixels = TILE_SIZE * (2**grid.zoom)
    global_columns = grid.min_x * TILE_SIZE + sampled_columns + 0.5
    global_rows = grid.min_y * TILE_SIZE + sampled_rows + 0.5

    world_size_m = 2.0 * WEB_MERCATOR_HALF_WORLD
    x_3857 = global_columns / world_pixels * world_size_m - WEB_MERCATOR_HALF_WORLD
    y_3857 = WEB_MERCATOR_HALF_WORLD - global_rows / world_pixels * world_size_m
    return x_3857, y_3857


def polygon_cell_mask(polygon_local, centre_x: np.ndarray, centre_y: np.ndarray):
    """Return a boolean mask for cell centres inside the polygon.

    Shapely 2 offers a fast vectorised contains_xy implementation. The fallback
    keeps compatibility with Shapely 1.x.
    """

    try:
        from shapely import contains_xy

        return contains_xy(polygon_local, centre_x, centre_y)
    except ImportError:
        prepared_polygon = prep(polygon_local)
        flat_mask = np.fromiter(
            (
                prepared_polygon.contains(Point(float(x), float(y)))
                for x, y in zip(centre_x.ravel(), centre_y.ravel())
            ),
            dtype=bool,
            count=centre_x.size,
        )
        return flat_mask.reshape(centre_x.shape)


def build_clipped_mesh(
    mosaic: np.ndarray,
    grid: TileGrid,
    polygon_wgs84,
    local_crs: CRS,
    sample_step: int,
    vertical_exaggeration: float,
) -> tuple[trimesh.Trimesh, dict[str, Any]]:
    """Build a metric terrain mesh and clip its cells to the polygon."""

    # Retain regularly spaced source pixels. Including the final row/column
    # prevents a large missing strip when the dimensions are not divisible by step.
    source_rows = np.arange(0, mosaic.shape[0], sample_step, dtype=np.int64)
    source_columns = np.arange(0, mosaic.shape[1], sample_step, dtype=np.int64)
    if source_rows[-1] != mosaic.shape[0] - 1:
        source_rows = np.append(source_rows, mosaic.shape[0] - 1)
    if source_columns[-1] != mosaic.shape[1] - 1:
        source_columns = np.append(source_columns, mosaic.shape[1] - 1)

    elevations = mosaic[np.ix_(source_rows, source_columns)]
    row_grid, column_grid = np.meshgrid(source_rows, source_columns, indexing="ij")

    x_3857, y_3857 = global_pixel_centres_web_mercator(
        grid,
        row_grid,
        column_grid,
    )

    # Transform exact Web-Mercator pixel positions into the chosen local CRS.
    to_local = Transformer.from_crs("EPSG:3857", local_crs, always_xy=True)
    projected_x, projected_y = to_local.transform(x_3857, y_3857)

    # Transform the clipping polygon into precisely the same CRS.
    from_wgs84 = Transformer.from_crs("EPSG:4326", local_crs, always_xy=True)
    polygon_projected = shapely_transform(from_wgs84.transform, polygon_wgs84)

    # Use a local origin to avoid floating-point precision problems in 3D engines.
    origin_x = float(polygon_projected.bounds[0])
    origin_y = float(polygon_projected.bounds[1])
    local_x = projected_x - origin_x
    local_y = projected_y - origin_y
    polygon_local = shapely_transform(
        lambda x, y, z=None: (x - origin_x, y - origin_y),
        polygon_projected,
    )

    # A grid cell is included if its centre lies inside the polygon. This supports
    # concave polygons and holes while retaining a regular terrain triangulation.
    centre_x = (
        local_x[:-1, :-1]
        + local_x[:-1, 1:]
        + local_x[1:, :-1]
        + local_x[1:, 1:]
    ) / 4.0
    centre_y = (
        local_y[:-1, :-1]
        + local_y[:-1, 1:]
        + local_y[1:, :-1]
        + local_y[1:, 1:]
    ) / 4.0
    keep_cells = polygon_cell_mask(polygon_local, centre_x, centre_y)

    if not np.any(keep_cells):
        raise ValueError(
            "The sampling interval is too coarse for this polygon; reduce --sample-step."
        )

    # The vertical origin is the minimum elevation used by retained cells.
    involved = np.zeros(elevations.shape, dtype=bool)
    involved[:-1, :-1] |= keep_cells
    involved[:-1, 1:] |= keep_cells
    involved[1:, :-1] |= keep_cells
    involved[1:, 1:] |= keep_cells
    elevation_origin = float(np.nanmin(elevations[involved]))
    local_z = (elevations - elevation_origin) * vertical_exaggeration

    rows, columns = elevations.shape
    vertex_ids = np.arange(rows * columns, dtype=np.int64).reshape(rows, columns)
    cell_rows, cell_columns = np.nonzero(keep_cells)

    top_left = vertex_ids[cell_rows, cell_columns]
    top_right = vertex_ids[cell_rows, cell_columns + 1]
    bottom_left = vertex_ids[cell_rows + 1, cell_columns]
    bottom_right = vertex_ids[cell_rows + 1, cell_columns + 1]

    # Winding order is chosen so triangle normals point upward.
    faces_a = np.column_stack((top_left, bottom_left, top_right))
    faces_b = np.column_stack((top_right, bottom_left, bottom_right))
    faces = np.vstack((faces_a, faces_b))

    vertices = np.column_stack(
        (local_x.ravel(), local_y.ravel(), local_z.ravel())
    ).astype(np.float32)

    mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
    mesh.remove_unreferenced_vertices()
    # Current Trimesh exposes face-quality tests as masks. Updating faces with
    # those masks removes zero-area and duplicate triangles before normals are
    # generated.
    mesh.update_faces(mesh.nondegenerate_faces())
    mesh.update_faces(mesh.unique_faces())
    mesh.remove_unreferenced_vertices()
    # Face winding was constructed explicitly with upward-facing normals, so
    # Trimesh's SciPy-backed multi-body normal repair is unnecessary here.

    retained_elevations = elevations[involved]
    metadata = {
        "source": "Mapterhorn",
        "source_url": "https://mapterhorn.com/",
        "tile_encoding": "Terrarium RGB in 512 px WebP",
        "zoom": grid.zoom,
        "tile_grid": {
            "min_x": grid.min_x,
            "max_x": grid.max_x,
            "min_y": grid.min_y,
            "max_y": grid.max_y,
            "count": grid.count,
        },
        "local_crs": local_crs.to_string(),
        "origin": {
            "x_m": origin_x,
            "y_m": origin_y,
            "elevation_m": elevation_origin,
        },
        "sample_step_pixels": sample_step,
        "vertical_exaggeration": vertical_exaggeration,
        "elevation_m": {
            "minimum": float(np.nanmin(retained_elevations)),
            "maximum": float(np.nanmax(retained_elevations)),
        },
        "mesh": {
            "vertices": int(len(mesh.vertices)),
            "triangles": int(len(mesh.faces)),
            "bounds_local_m": mesh.bounds.tolist(),
        },
        "note": (
            "GLB coordinates are local metres. Recover projected X/Y by adding "
            "origin.x_m/origin.y_m. Recover unexaggerated altitude by dividing "
            "local Z by vertical_exaggeration and adding origin.elevation_m."
        ),
    }
    return mesh, metadata

def mosaic_web_mercator_transform(grid: TileGrid) -> Affine:
    """Return the EPSG:3857 affine transform of the full tile mosaic.

    The transform maps raster pixel corners—not centres—to Web Mercator metres.
    Rasterio consequently locates each sample at the same +0.5 pixel centre used
    by :func:`global_pixel_centres_web_mercator`.
    """

    world_pixels = TILE_SIZE * (2**grid.zoom)
    resolution = (2.0 * WEB_MERCATOR_HALF_WORLD) / world_pixels
    west = (
        -WEB_MERCATOR_HALF_WORLD
        + grid.min_x * TILE_SIZE * resolution
    )
    north = (
        WEB_MERCATOR_HALF_WORLD
        - grid.min_y * TILE_SIZE * resolution
    )
    return Affine(resolution, 0.0, west, 0.0, -resolution, north)



def export_elevation_geotiff(
    mosaic: np.ndarray,
    grid: TileGrid,
    polygon_wgs84,
    local_crs: CRS,
    output_path: Path,
) -> dict[str, Any]:
    """Reproject, polygon-mask, and save the full elevation grid as GeoTIFF.

    The source mosaic is natively aligned to EPSG:3857 XYZ pixels. It is first
    masked by the requested polygon, then bilinearly reprojected into the same
    projected CRS used for the GLB. ``sample_step`` is intentionally not used:
    the GeoTIFF remains the full analytical grid while the GLB can be simplified.
    """

    source_crs = CRS.from_epsg(3857)
    source_transform = mosaic_web_mercator_transform(grid)
    source_height, source_width = mosaic.shape

    to_web_mercator = Transformer.from_crs(
        "EPSG:4326",
        source_crs,
        always_xy=True,
    )
    polygon_3857 = shapely_transform(
        to_web_mercator.transform,
        polygon_wgs84,
    )

    # True means that the pixel centre lies inside the polygon. Pixels outside
    # it (and pixels inside polygon holes) receive the GeoTIFF NoData value.
    source_inside = geometry_mask(
        [mapping(polygon_3857)],
        out_shape=mosaic.shape,
        transform=source_transform,
        invert=True,
        all_touched=False,
    )
    source_data = np.where(
        source_inside,
        mosaic,
        GEOTIFF_NODATA,
    ).astype(np.float32)

    left, bottom, right, top = rasterio.transform.array_bounds(
        source_height,
        source_width,
        source_transform,
    )
    destination_transform, destination_width, destination_height = (
        calculate_default_transform(
            source_crs,
            local_crs,
            source_width,
            source_height,
            left,
            bottom,
            right,
            top,
        )
    )

    destination = np.full(
        (destination_height, destination_width),
        GEOTIFF_NODATA,
        dtype=np.float32,
    )
    reproject(
        source=source_data,
        destination=destination,
        src_transform=source_transform,
        src_crs=source_crs,
        src_nodata=GEOTIFF_NODATA,
        dst_transform=destination_transform,
        dst_crs=local_crs,
        dst_nodata=GEOTIFF_NODATA,
        resampling=Resampling.bilinear,
        num_threads=2,
    )

    # Reapply the polygon in the destination grid. This prevents interpolation
    # at the reprojection boundary from creating values outside the requested AOI.
    to_local = Transformer.from_crs("EPSG:4326", local_crs, always_xy=True)
    polygon_projected = shapely_transform(to_local.transform, polygon_wgs84)
    destination_inside = geometry_mask(
        [mapping(polygon_projected)],
        out_shape=destination.shape,
        transform=destination_transform,
        invert=True,
        all_touched=False,
    )
    destination[~destination_inside] = GEOTIFF_NODATA

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(
        output_path,
        "w",
        driver="GTiff",
        height=destination_height,
        width=destination_width,
        count=1,
        dtype="float32",
        crs=local_crs,
        transform=destination_transform,
        nodata=GEOTIFF_NODATA,
        compress="deflate",
        predictor=3,
        BIGTIFF="IF_SAFER",
    ) as dataset:
        dataset.write(destination, 1)
        dataset.set_band_description(1, "elevation_m")
        dataset.update_tags(
            source="Mapterhorn",
            units="metres",
            area_mask="GeoJSON polygon",
        )

    valid = destination != GEOTIFF_NODATA
    if not np.any(valid):
        raise ValueError("No valid elevation cells remain in the GeoTIFF.")

    return {
        "path": str(output_path),
        "crs": local_crs.to_string(),
        "width": int(destination_width),
        "height": int(destination_height),
        "resolution_m": {
            "x": float(abs(destination_transform.a)),
            "y": float(abs(destination_transform.e)),
        },
        "nodata": GEOTIFF_NODATA,
        "valid_cells": int(np.count_nonzero(valid)),
        "elevation_m": {
            "minimum": float(np.min(destination[valid])),
            "maximum": float(np.max(destination[valid])),
        },
    }

def generate_terrain(
    geojson_path: Path,
    output_path: Path,
    zoom: int,
    sample_step: int,
    vertical_exaggeration: float,
    crs_override: str | None,
    max_tiles: int,
    workers: int,
    timeout: float,
) -> tuple[Path, Path]:
    """Generate terrain from a GeoJSON file.

    This convenience wrapper reads the file and delegates to
    :func:`generate_terrain_from_geojson`. Applications that already have a
    decoded GeoJSON object should call that function directly.
    """

    with geojson_path.open("r", encoding="utf-8") as file:
        geojson = json.load(file)

    return generate_terrain_from_geojson(
        geojson=geojson,
        output_path=output_path,
        zoom=zoom,
        sample_step=sample_step,
        vertical_exaggeration=vertical_exaggeration,
        crs=crs_override,
        max_tiles=max_tiles,
        workers=workers,
        timeout=timeout,
    )


def generate_terrain_from_geojson(
    geojson: dict[str, Any],
    output_path: str | Path,
    *,
    zoom: int = 5,
    sample_step: int = 4,
    vertical_exaggeration: float = 1.0,
    crs: str | None = None,
    max_tiles: int = 64,
    workers: int = 8,
    timeout: float = 30.0,
) -> tuple[Path, Path]:
    """Generate a GLB terrain mesh directly from a decoded GeoJSON object.

    Parameters
    ----------
    geojson:
        GeoJSON Polygon, MultiPolygon, Feature, or FeatureCollection in
        longitude/latitude coordinates (EPSG:4326).
    output_path:
        Destination GLB path. A metadata file with suffix ``.metadata.json``
        is written next to it.
    zoom:
        Mapterhorn XYZ zoom in the inclusive range 0..18. Higher values request
        denser source samples but do not improve the underlying source DEM.
    sample_step:
        Retain one Mapterhorn pixel in every N pixels. A smaller value produces
        a denser mesh and uses more memory.
    vertical_exaggeration:
        Multiplier applied to mesh Z only. Keep 1.0 for geometrically correct
        terrain.
    crs:
        Optional projected CRS such as ``EPSG:25829``. When omitted, the WGS84
        UTM zone containing the polygon centroid is selected automatically.
    max_tiles:
        Safety limit preventing unexpectedly large downloads.
    workers:
        Maximum number of concurrent HTTP tile downloads.
    timeout:
        Timeout in seconds for each tile request.

    Returns
    -------
    tuple[Path, Path]
        Paths to the generated GLB and its JSON metadata sidecar.
    """

    output_path = Path(output_path)
    if output_path.suffix.lower() != ".glb":
        raise ValueError("output_path must end in .glb")
    if not 0 <= zoom <= 18:
        raise ValueError("zoom must be between 0 and 18")
    if sample_step <= 0:
        raise ValueError("sample_step must be greater than zero")
    if vertical_exaggeration <= 0:
        raise ValueError("vertical_exaggeration must be greater than zero")
    if max_tiles <= 0 or workers <= 0 or timeout <= 0:
        raise ValueError("max_tiles, workers, and timeout must be greater than zero")

    # Get polygon coords
    polygon_wgs84 = parse_geojson_geometry(geojson)

    #Transform global coords to local coordinate reference system
    local_crs = CRS.from_user_input(crs) if crs else choose_local_crs(
        polygon_wgs84
    )
    if not local_crs.is_projected:
        raise ValueError("--crs must specify a projected metre-based CRS.")

    #Build tile grid
    grid = make_tile_grid(polygon_wgs84, zoom)
    if grid.count > max_tiles:
        raise ValueError(
            f"The area needs {grid.count} tiles at zoom {zoom}, exceeding "
            f"--max-tiles={max_tiles}. Reduce --zoom or increase the limit knowingly."
        )

    estimated_vertices = math.ceil(grid.rows * TILE_SIZE / sample_step) * math.ceil(
        grid.columns * TILE_SIZE / sample_step
    )
    print(
        f"Using {grid.count} tile(s), {local_crs.to_string()}, and approximately "
        f"{estimated_vertices:,} pre-clipping vertices.",
        file=sys.stderr,
    )

    mosaic = download_mosaic(grid, workers=workers, timeout=timeout)

    geotiff_path = output_path.with_suffix(".tif")
    geotiff_metadata = export_elevation_geotiff(
        mosaic=mosaic,
        grid=grid,
        polygon_wgs84=polygon_wgs84,
        local_crs=local_crs,
        output_path=geotiff_path,
    )


    mesh, metadata = build_clipped_mesh(
        mosaic=mosaic,
        grid=grid,
        polygon_wgs84=polygon_wgs84,
        local_crs=local_crs,
        sample_step=sample_step,
        vertical_exaggeration=vertical_exaggeration,
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    mesh.export(output_path, file_type="glb")

    metadata_path = output_path.with_suffix(".metadata.json")
    metadata["files"] = {
        "glb": str(output_path),
        "geotiff": str(geotiff_path),
        "metadata": str(metadata_path),
    }
    metadata["geotiff"] = geotiff_metadata
    with metadata_path.open("w", encoding="utf-8") as file:
        json.dump(metadata, file, indent=2)
        file.write("\n")

    return output_path, geotiff_path, metadata_path


