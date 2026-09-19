"""DEM difference (post minus pre) on the analysis grid.

Both DEMs are put on the grid the config defines, with the same warp and the
same resampling the pipeline uses for its DEM, then differenced cell by cell.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import rasterio

from landlab_data_prep.analysis_grid import DEM_RESAMPLING, NODATA, Grid, align_to_grid, check_on_grid, grid_from_config
from landlab_data_prep.config import ConfigError, load_config
from landlab_data_prep.reproject_and_resample import convert_to_ascii


def compute_difference(pre_tif: Path, post_tif: Path, diff_tif: Path, grid: Grid) -> Path:
    """post - pre, with nodata wherever either DEM is missing."""
    check_on_grid(pre_tif, grid)
    check_on_grid(post_tif, grid)
    with rasterio.open(pre_tif) as pre, rasterio.open(post_tif) as post:
        pre_arr = pre.read(1)
        post_arr = post.read(1)
        missing = (
            (pre_arr == pre.nodata)
            | (post_arr == post.nodata)
            | ~np.isfinite(pre_arr)
            | ~np.isfinite(post_arr)
        )
    diff = np.where(missing, NODATA, post_arr - pre_arr).astype("float32")
    with rasterio.open(diff_tif, "w", **grid.profile()) as dst:
        dst.write(diff, 1)
    return diff_tif


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Compute DEM difference (post - pre) on the analysis grid and export ASCII grids."
    )
    parser.add_argument("--pre", required=True, help="Path to pre-event DEM GeoTIFF.")
    parser.add_argument("--post", required=True, help="Path to post-event DEM GeoTIFF.")
    parser.add_argument("--out-dir", required=True, help="Output directory.")
    parser.add_argument("--config", required=True, help="Config that defines the AOI and analysis grid.")
    args = parser.parse_args(argv)

    try:
        cfg = load_config(args.config, "dem_difference")
    except (ConfigError, FileNotFoundError) as exc:
        raise SystemExit(str(exc)) from None
    grid = grid_from_config(cfg)
    aoi_path = cfg["aoi"]["aoi"]
    out_dir = Path(args.out_dir)

    pre_tif = align_to_grid(args.pre, out_dir / "dem_pre.tif", grid, DEM_RESAMPLING, aoi_path=aoi_path)
    post_tif = align_to_grid(args.post, out_dir / "dem_post.tif", grid, DEM_RESAMPLING, aoi_path=aoi_path)
    diff_tif = compute_difference(pre_tif, post_tif, out_dir / "dem_diff.tif", grid)

    for tif in (pre_tif, post_tif, diff_tif):
        print("Saved:", tif, convert_to_ascii(str(tif), str(out_dir), grid=grid))


if __name__ == "__main__":
    main()
