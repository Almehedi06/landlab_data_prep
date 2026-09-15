"""The one analysis grid, and the one way onto it.

Every command derives the grid from the main config with ``grid_from_config``
and puts layers on it with ``align_to_grid``. Nothing else in the repo chooses
a CRS, an origin or a pixel size, and nothing else resamples.

The grid rule:
  CRS     the AOI's own CRS when it is UTM, otherwise the UTM zone of its centre
  extent  the AOI bounds, snapped outward to multiples of raster.target_res
  pixels  square, raster.target_res metres
The AOI file is only read, never rewritten.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
from typing import Callable

import numpy as np
import rasterio
from rasterio.crs import CRS
from rasterio.enums import Resampling
from rasterio.features import geometry_mask
from rasterio.transform import Affine
from rasterio.vrt import WarpedVRT

from preflight import load_and_validate_aoi

NODATA = -9999.0
ALIGNED_SUBDIR = "_aligned"
DEM_RESAMPLING = "cubic"
RESAMPLING = {
    "nearest": Resampling.nearest,
    "bilinear": Resampling.bilinear,
    "cubic": Resampling.cubic,
    "average": Resampling.average,
    "mode": Resampling.mode,
}
# Distance from a pixel edge, as a fraction of a pixel, treated as float noise.
_SNAP_TOLERANCE = 1e-6


class GridMismatchError(ValueError):
    pass


@dataclass(frozen=True)
class Grid:
    crs: str
    transform: Affine
    width: int
    height: int

    @property
    def shape(self) -> tuple[int, int]:
        return (self.height, self.width)

    @property
    def resolution(self) -> float:
        return float(self.transform.a)

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        left, top = float(self.transform.c), float(self.transform.f)
        return (left, top - self.height * self.resolution, left + self.width * self.resolution, top)

    def profile(self, dtype: str = "float32", nodata: float = NODATA) -> dict:
        return {
            "driver": "GTiff",
            "width": self.width,
            "height": self.height,
            "count": 1,
            "dtype": dtype,
            "crs": self.crs,
            "transform": self.transform,
            "nodata": nodata,
            "compress": "deflate",
        }

    def as_dict(self) -> dict:
        return {
            "crs": self.crs,
            "resolution": self.resolution,
            "width": self.width,
            "height": self.height,
            "transform": [float(v) for v in tuple(self.transform)[:6]],
        }


def resampling_method(name: str) -> Resampling:
    try:
        return RESAMPLING[name]
    except (KeyError, TypeError):
        raise ValueError(f"Unknown resampling {name!r}; choose one of {sorted(RESAMPLING)}") from None


def utm_crs_for(aoi) -> str:
    """The AOI's own CRS when it is UTM, otherwise the UTM zone of the AOI centre."""
    if aoi.crs is None:
        raise ValueError("AOI has no CRS")
    epsg = aoi.crs.to_epsg()
    if aoi.crs.utm_zone is not None and epsg is not None:
        return f"EPSG:{epsg}"
    west, south, east, north = aoi.to_crs(epsg=4326).total_bounds
    lon, lat = (west + east) / 2.0, (south + north) / 2.0
    zone = min(int((lon + 180.0) // 6) + 1, 60)
    return f"EPSG:{326 if lat >= 0 else 327}{zone:02d}"


def snap_grid(bounds: tuple[float, float, float, float], crs: str, resolution: float) -> Grid:
    """Smallest grid covering ``bounds`` whose edges are multiples of ``resolution``."""
    if not resolution > 0:
        raise ValueError(f"Resolution must be positive, got {resolution}")
    left, bottom, right, top = (float(v) for v in bounds)
    if not (right > left and top > bottom):
        raise ValueError(f"Degenerate bounds {bounds}")
    x0 = _snap(left, resolution, math.floor)
    x1 = _snap(right, resolution, math.ceil)
    y0 = _snap(bottom, resolution, math.floor)
    y1 = _snap(top, resolution, math.ceil)
    return Grid(
        crs=crs,
        transform=Affine(resolution, 0.0, x0, 0.0, -resolution, y1),
        width=int(round((x1 - x0) / resolution)),
        height=int(round((y1 - y0) / resolution)),
    )


def _snap(value: float, resolution: float, direction: Callable[[float], int]) -> float:
    q = value / resolution
    nearest = round(q)
    if abs(q - nearest) <= _SNAP_TOLERANCE:
        return nearest * resolution
    return direction(q) * resolution


def grid_for_aoi(aoi_path: str | Path, resolution: float) -> Grid:
    aoi = load_and_validate_aoi(aoi_path)
    crs = utm_crs_for(aoi)
    return snap_grid(tuple(aoi.to_crs(crs).total_bounds), crs, resolution)


def grid_from_config(cfg: dict) -> Grid:
    """The analysis grid every command uses. Reads aoi.aoi and raster.target_res only."""
    problems: list[str] = []
    aoi_path = (cfg.get("aoi") or {}).get("aoi")
    if not aoi_path:
        problems.append("aoi.aoi is required")
    resolution = (cfg.get("raster") or {}).get("target_res")
    if isinstance(resolution, bool) or not isinstance(resolution, (int, float)) or not resolution > 0:
        problems.append(f"raster.target_res must be a positive number of metres, got {resolution!r}")
    if problems:
        raise ValueError("Cannot build the analysis grid: " + "; ".join(problems))
    return grid_for_aoi(aoi_path, float(resolution))


def read_on_grid(
    path: str | Path,
    grid: Grid,
    resampling: str,
    *,
    dtype: str | None = None,
    nodata: float | None = None,
    default_src_nodata: float | None = None,
) -> tuple[np.ndarray, float | None, float | None]:
    """Warp band 1 of ``path`` onto ``grid``. The single warp used everywhere.

    Returns (array, source nodata, declared scale). Without ``dtype`` and
    ``nodata`` the array keeps the source's own type and nodata value.
    """
    method = resampling_method(resampling)
    with rasterio.open(path) as src:
        if src.crs is None:
            raise ValueError(f"{path} has no CRS")
        src_nodata = src.nodata if src.nodata is not None else default_src_nodata
        with WarpedVRT(
            src,
            crs=grid.crs,
            transform=grid.transform,
            width=grid.width,
            height=grid.height,
            resampling=method,
            src_nodata=src_nodata,
            nodata=src_nodata if nodata is None else nodata,
            dtype=dtype or src.dtypes[0],
        ) as vrt:
            data = vrt.read(1)
        scale = src.scales[0] if src.scales else None
    return data, src_nodata, scale


def aoi_mask(aoi_path: str | Path, grid: Grid) -> np.ndarray:
    """True for grid cells whose centre falls inside the AOI."""
    aoi = load_and_validate_aoi(aoi_path).to_crs(grid.crs)
    return geometry_mask(list(aoi.geometry), out_shape=grid.shape, transform=grid.transform, invert=True)


def align_to_grid(
    src_path: str | Path,
    dst_path: str | Path,
    grid: Grid,
    resampling: str,
    *,
    aoi_path: str | Path | None = None,
    tags: dict[str, str] | None = None,
) -> Path:
    """Put band 1 of ``src_path`` on ``grid`` as float32 with NODATA, masked outside the AOI."""
    dst_path = Path(dst_path)
    if Path(src_path).resolve() == dst_path.resolve():
        raise ValueError(f"Refusing to overwrite the source while aligning: {src_path}")

    data, _, _ = read_on_grid(src_path, grid, resampling, dtype="float32", nodata=NODATA)
    data[~np.isfinite(data)] = NODATA
    if aoi_path is not None:
        data[~aoi_mask(aoi_path, grid)] = NODATA
    if not np.any(data != NODATA):
        raise ValueError(f"{src_path} has no valid pixels on the analysis grid. Does it cover the AOI?")

    dst_path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(dst_path, "w", **grid.profile()) as dst:
        dst.write(data, 1)
        if tags:
            dst.update_tags(**tags)
    check_on_grid(dst_path, grid)
    return dst_path


_ASCII_HEADER_KEYS = {"ncols", "nrows", "xllcorner", "yllcorner", "xllcenter", "yllcenter", "cellsize", "nodata_value"}


def read_ascii_header(path: str | Path) -> dict[str, float]:
    """Header of an ESRI ASCII grid; handles 5-line headers without NODATA_value."""
    header: dict[str, float] = {}
    with open(path, "r") as f:
        for _ in range(len(_ASCII_HEADER_KEYS)):
            parts = f.readline().split()
            if len(parts) < 2 or parts[0].lower() not in _ASCII_HEADER_KEYS:
                break
            header[parts[0].lower()] = float(parts[1])
    return header


def check_on_grid(path: str | Path, grid: Grid) -> None:
    """Raise GridMismatchError unless the GeoTIFF or ESRI ASCII file sits exactly on ``grid``."""
    path = Path(path)
    tol = grid.resolution * 1e-6
    problems: list[str] = []

    if path.suffix.lower() == ".asc":
        header = read_ascii_header(path)
        left, bottom, _, _ = grid.bounds
        expected = {
            "ncols": grid.width,
            "nrows": grid.height,
            "cellsize": grid.resolution,
            "xllcorner": left,
            "yllcorner": bottom,
        }
        for key, want in expected.items():
            got = header.get(key)
            if got is None or abs(got - want) > tol:
                problems.append(f"{key} {got} != {want}")
    else:
        with rasterio.open(path) as src:
            if (src.width, src.height) != (grid.width, grid.height):
                problems.append(f"shape {src.width}x{src.height} != {grid.width}x{grid.height}")
            if not src.transform.almost_equals(grid.transform, precision=tol):
                problems.append(f"transform {tuple(src.transform)[:6]} != {tuple(grid.transform)[:6]}")
            if src.crs is None or CRS.from_user_input(src.crs) != CRS.from_user_input(grid.crs):
                problems.append(f"crs {src.crs} != {grid.crs}")
            if src.nodata is None:
                problems.append("no declared nodata")

    if problems:
        raise GridMismatchError(f"{path} is not on the analysis grid: " + "; ".join(problems))
