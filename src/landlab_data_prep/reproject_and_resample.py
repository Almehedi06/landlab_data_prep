"""Raster file helpers: crop a raster to an AOI and write ESRI ASCII grids.

Nothing here reprojects or resamples. Putting a layer on the analysis grid is
``analysis_grid.align_to_grid``, the only warp in the repo.
"""

from __future__ import annotations

import os
from pathlib import Path

import fiona
import numpy as np
import rasterio
from rasterio.crs import CRS
from rasterio.mask import mask
from rasterio.transform import array_bounds
from rasterio.warp import transform_geom

from landlab_data_prep.analysis_grid import Grid, check_on_grid, read_ascii_header

__all__ = ["clip_raster_to_shape", "convert_to_ascii", "read_ascii_header"]


def _read_shapes(
    shapefile_path: str,
    raster_crs,
    reproject_shapes: bool = True,
) -> list[dict]:
    with fiona.open(shapefile_path, "r") as src:
        shapes = [feature["geometry"] for feature in src]
        if not reproject_shapes:
            return shapes

        src_crs = src.crs_wkt or src.crs
        if not src_crs or raster_crs is None:
            return shapes

        src_crs_obj = CRS.from_user_input(src_crs)
        dst_crs_obj = CRS.from_user_input(raster_crs)
        if src_crs_obj == dst_crs_obj:
            return shapes

        return [transform_geom(src_crs_obj, dst_crs_obj, geom) for geom in shapes]


def clip_raster_to_shape(raster_path: str, shapefile_path: str) -> str:
    """Crop a raster to the AOI and mask outside it, on the raster's own grid.

    Used to cut large source rasters down before alignment. It is not an
    alignment step and never changes pixel size or origin.
    """
    with rasterio.open(raster_path) as src:
        shapes = _read_shapes(shapefile_path, src.crs)
        out_image, out_transform = mask(src, shapes, crop=True)
        out_meta = src.meta.copy()

    out_meta.update(
        {
            "driver": "GTiff",
            "height": out_image.shape[1],
            "width": out_image.shape[2],
            "transform": out_transform,
        }
    )
    root, _ = os.path.splitext(str(raster_path))
    clipped_path = f"{root}_clipped.tif"
    with rasterio.open(clipped_path, "w", **out_meta) as dest:
        dest.write(out_image)
    return clipped_path


def convert_to_ascii(tif_path: str, out_dir: str, grid: Grid | None = None) -> str:
    """Write band 1 as an ESRI ASCII grid named after the input file.

    With ``grid`` the input must already sit on it; a mismatch raises instead of
    writing a grid that silently disagrees with the others.
    """
    os.makedirs(out_dir, exist_ok=True)
    if grid is not None:
        check_on_grid(tif_path, grid)

    with rasterio.open(tif_path) as src:
        array = src.read(1)
        width, height, transform = src.width, src.height, src.transform
        nodata_value = src.nodata if src.nodata is not None else -9999.0

    west, south, _, _ = array_bounds(height, width, transform)
    ascii_path = os.path.join(out_dir, f"{Path(tif_path).stem}.asc")
    with open(ascii_path, "w") as f:
        f.write(f"ncols         {width}\n")
        f.write(f"nrows         {height}\n")
        f.write(f"xllcorner     {west}\n")
        f.write(f"yllcorner     {south}\n")
        f.write(f"cellsize      {abs(transform[0])}\n")
        f.write(f"NODATA_value  {nodata_value}\n")
        # Vectorised: a per-cell Python loop is minutes per layer on 10 m fire grids.
        if np.issubdtype(array.dtype, np.floating):
            np.savetxt(f, np.where(np.isnan(array), nodata_value, array), fmt="%.9g")
        else:
            np.savetxt(f, array, fmt="%d")

    if grid is not None:
        check_on_grid(ascii_path, grid)
    return ascii_path
