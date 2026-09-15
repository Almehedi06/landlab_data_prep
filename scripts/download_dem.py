#!/usr/bin/env python3
"""Download OpenTopography elevation and write an aligned, single-band GeoTIFF.

Dependencies: bmi-topography, geopandas, rasterio, shapely, numpy, pyyaml.
This file is standalone: it does not import other modules from this repository.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
from pathlib import Path
import tempfile

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.features import geometry_mask
from rasterio.transform import from_origin
from rasterio.vrt import WarpedVRT
from rasterio.warp import transform_bounds
from shapely.geometry import Polygon

NODATA = -9999.0


def resolve_api_key(config: Path | None = None) -> str:
    key = os.environ.get("USGS_TOPO_API_KEY") or os.environ.get("OPENTOPOGRAPHY_API_KEY")
    if key and key.strip():
        return key.strip()
    path = config if config is not None else Path(__file__).resolve().parents[1] / "config/base.yaml"
    if path.exists():
        import yaml
        cfg = yaml.safe_load(path.read_text()) or {}
        key = (cfg.get("dem") or {}).get("api_key")
        if isinstance(key, str) and key.strip():
            return key.strip()
    elif config is not None:
        raise ValueError(f"Config not found: {config}")
    raise ValueError("Set dem.api_key in --config (default: config/base.yaml beside the scripts directory), "
                     "or set USGS_TOPO_API_KEY or OPENTOPOGRAPHY_API_KEY")


def read_aoi(path: Path) -> gpd.GeoDataFrame:
    """A raster contributes its full footprint; a vector contributes its geometry."""
    try:
        src = rasterio.open(path)
    except rasterio.errors.RasterioIOError:
        aoi = gpd.read_file(path)
    else:
        with src:
            if src.crs is None:
                raise ValueError(f"Raster AOI has no CRS: {path}")
            corners = [src.transform * p for p in
                       [(0, 0), (src.width, 0), (src.width, src.height), (0, src.height)]]
            aoi = gpd.GeoDataFrame(geometry=[Polygon(corners)], crs=src.crs)
    if aoi.crs is None or aoi.empty:
        raise ValueError("AOI must contain geometry and a CRS")
    if aoi.geometry.isna().any() or aoi.geometry.is_empty.any() or not aoi.geometry.is_valid.all():
        raise ValueError("AOI contains null, empty, or invalid geometry")
    if not aoi.geom_type.isin(["Polygon", "MultiPolygon"]).all():
        raise ValueError("AOI must contain polygons")
    return aoi


def target_profile(aoi, reference: Path | None, crs: str | None, resolution: float | None) -> dict:
    if reference:
        if crs is not None or resolution is not None:
            raise ValueError("--reference defines the CRS and resolution; omit --crs and --resolution")
        with rasterio.open(reference) as src:
            if src.crs is None:
                raise ValueError("Reference raster has no CRS")
            profile = dict(crs=src.crs, transform=src.transform, width=src.width, height=src.height)
    else:
        if crs is None or resolution is None or not math.isfinite(resolution) or resolution <= 0:
            raise ValueError("Without --reference, provide --crs and a finite positive --resolution")
        target = rasterio.crs.CRS.from_user_input(crs)
        left, bottom, right, top = aoi.to_crs(target).total_bounds
        left = math.floor(left / resolution) * resolution
        bottom = math.floor(bottom / resolution) * resolution
        right = math.ceil(right / resolution) * resolution
        top = math.ceil(top / resolution) * resolution
        profile = dict(crs=target, transform=from_origin(left, top, resolution, resolution),
                       width=int(round((right-left)/resolution)), height=int(round((top-bottom)/resolution)))
    return dict(profile, driver="GTiff", count=1, dtype="float32", nodata=NODATA,
                compress="deflate")


def write_dem(source: Path, output: Path, profile: dict, aoi, resampling: str,
              reference: Path | None = None, mask_reference: bool = False) -> dict:
    """Warp once to the exact destination grid and atomically publish the result."""
    if source.resolve() == output.resolve():
        raise ValueError("Output must differ from the source DEM")
    inside = geometry_mask(list(aoi.to_crs(profile["crs"]).geometry),
                           out_shape=(profile["height"], profile["width"]),
                           transform=profile["transform"], invert=True)
    if mask_reference:
        if reference is None:
            raise ValueError("--mask-reference requires --reference")
        with rasterio.open(reference) as ref:
            inside &= ref.read_masks(1) > 0
    if not inside.any():
        raise ValueError("AOI contains no pixel centres on the target grid")
    with rasterio.open(source) as src:
        if src.crs is None:
            raise ValueError("Source DEM has no CRS")
        with WarpedVRT(src, crs=profile["crs"], transform=profile["transform"],
                       width=profile["width"], height=profile["height"],
                       dtype="float32", nodata=NODATA, resampling=Resampling[resampling]) as vrt:
            data = vrt.read(1, masked=True).filled(NODATA)
    data[~inside | ~np.isfinite(data)] = NODATA
    valid = inside & (data != NODATA)
    if not valid.any():
        raise ValueError("Downloaded DEM has no valid elevation within the target AOI")
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(suffix=".tif", prefix=".dem_", dir=output.parent)
    os.close(fd)
    temporary = Path(name)
    try:
        with rasterio.open(temporary, "w", **profile) as dst:
            dst.write(data, 1)
            dst.update_tags(resampling=resampling, source_dem=str(source),
                            vertical_processing="Source elevations retained; no vertical datum conversion")
        with rasterio.open(temporary) as result:
            if (result.crs != profile["crs"] or result.transform != profile["transform"]
                    or result.width != profile["width"] or result.height != profile["height"]):
                raise ValueError("Output grid verification failed")
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    return {"output": str(output.resolve()), "crs": str(profile["crs"]),
            "width": profile["width"], "height": profile["height"],
            "transform": list(profile["transform"])[:6], "valid_pixels": int(valid.sum()),
            "missing_pixels_in_domain": int((inside & ~valid).sum())}


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--aoi", type=Path, help="Polygon vector or raster footprint; defaults to --reference")
    parser.add_argument("--reference", type=Path, help="Match this raster's exact CRS, transform, width and height")
    parser.add_argument("--crs", help="Target CRS, e.g. EPSG:32610 (without --reference)")
    parser.add_argument("--resolution", type=float, help="Square pixel size in target CRS units (without --reference)")
    parser.add_argument("--output", required=True, type=Path, help="Output GeoTIFF")
    parser.add_argument("--config", type=Path, help="Read dem.api_key only; defaults to this script's ../config/base.yaml")
    parser.add_argument("--dem-type", default="USGS10m", help="bmi-topography DEM product (default: USGS10m)")
    parser.add_argument("--cache-dir", type=Path, help="Download cache; default: <output parent>/dem_cache")
    parser.add_argument("--buffer-deg", type=float, default=0.01, help="Download margin in degrees (default: 0.01)")
    parser.add_argument("--resampling", choices=["nearest", "bilinear", "cubic", "average"], default="cubic")
    parser.add_argument("--mask-reference", action="store_true", help="Also mask nodata cells from reference band 1")
    parser.add_argument("--source-dem", type=Path, help="Use a local DEM instead of downloading (offline/reuse)")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)
    try:
        aoi_path = args.aoi or args.reference
        if aoi_path is None:
            raise ValueError("Provide --aoi or --reference")
        if args.output.exists() and not args.overwrite:
            raise ValueError(f"Output exists; use --overwrite: {args.output}")
        for source in [aoi_path, args.reference, args.source_dem]:
            if source and source.resolve() == args.output.resolve():
                raise ValueError("Output must differ from every input")
        if args.mask_reference and args.reference is None:
            raise ValueError("--mask-reference requires --reference")
        if not math.isfinite(args.buffer_deg) or args.buffer_deg < 0:
            raise ValueError("--buffer-deg must be finite and nonnegative")
        aoi = read_aoi(aoi_path)
        profile = target_profile(aoi, args.reference, args.crs, args.resolution)
        source = args.source_dem
        if source is None:
            from bmi_topography import Topography
            key = resolve_api_key(args.config)
            west, south, east, north = transform_bounds(aoi.crs, "EPSG:4326", *aoi.total_bounds, densify_pts=21)
            margin = args.buffer_deg
            if east-west > 180 or south-margin <= -90 or north+margin >= 90:
                raise ValueError("AOI spans unsupported antimeridian or polar bounds")
            cache = args.cache_dir or args.output.parent / "dem_cache"
            cache.mkdir(parents=True, exist_ok=True)
            source = Path(Topography(dem_type=args.dem_type, south=south-margin, north=north+margin,
                                    west=max(-180, west-margin), east=min(180, east+margin),
                                    output_format="GTiff", cache_dir=cache, api_key=key).fetch())
        report = write_dem(source, args.output, profile, aoi, args.resampling,
                           args.reference, args.mask_reference)
        print(json.dumps(report, indent=2))
    except (ValueError, OSError) as exc:
        message = re.sub(r"(?i)(API_Key=)[^&\s]+", r"\1[REDACTED]", str(exc))
        parser.exit(1, f"Error: {message}\n")


if __name__ == "__main__":
    main()
