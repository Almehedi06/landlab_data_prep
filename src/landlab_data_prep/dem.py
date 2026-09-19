"""DEM retrieval: a local GeoTIFF, or OpenTopography through bmi-topography."""

from __future__ import annotations

import os
from pathlib import Path
import re
from typing import Mapping

import geopandas as gpd
import rasterio
from bmi_topography import Topography

KEY_ENV_VARS = ("USGS_TOPO_API_KEY", "OPENTOPOGRAPHY_API_KEY")


class MissingApiKeyError(ValueError):
    pass


def resolve_api_key(dem_cfg: Mapping, environ: Mapping[str, str] | None = None) -> str:
    """Environment first, then dem.api_key: the same order as scripts/download_dem.py.

    Raises instead of letting bmi-topography fall back to its public demo key.
    """
    environ = os.environ if environ is None else environ
    for var in KEY_ENV_VARS:
        value = (environ.get(var) or "").strip()
        if value:
            return value
    value = str(dem_cfg.get("api_key") or "").strip()
    if value:
        return value
    raise MissingApiKeyError(
        "Downloading a DEM needs an OpenTopography API key. Set dem.api_key in the config, "
        "or the USGS_TOPO_API_KEY or OPENTOPOGRAPHY_API_KEY environment variable."
    )


def fetch_dem(aoi_path: str | Path, dem_cfg: Mapping, cache_dir: str | Path | None) -> Path:
    if dem_cfg["source"] == "local":
        path = Path(dem_cfg["path"])
        if not path.is_file():
            raise FileNotFoundError(f"Local DEM not found: {path}")
        return path

    if cache_dir is None:
        raise ValueError("Downloading a DEM needs paths.cache_dir in the config.")
    key = resolve_api_key(dem_cfg)
    west, south, east, north = gpd.read_file(aoi_path).to_crs(epsg=4326).total_bounds
    buffer = float(dem_cfg["buffer_deg"])
    topo = Topography(
        dem_type=dem_cfg["dem_type"],
        south=south - buffer,
        north=north + buffer,
        west=west - buffer,
        east=east + buffer,
        output_format="GTiff",
        cache_dir=str(cache_dir),
        api_key=key,
    )

    # bmi-topography reuses any cached file with the right name, including an
    # empty one left by an interrupted download. Check it and download once more.
    problem = None
    for _ in range(2):
        try:
            path = Path(topo.fetch())
        except Exception as exc:
            raise RuntimeError(f"DEM download failed: {_redact(str(exc), key)}") from None
        problem = _dem_problem(path)
        if problem is None:
            return path
        path.unlink(missing_ok=True)
    raise RuntimeError(f"DEM download produced an unusable file twice ({problem}): {path}")


def _dem_problem(path: Path) -> str | None:
    if not path.is_file() or path.stat().st_size == 0:
        return "empty file"
    try:
        with rasterio.open(path) as src:
            if src.crs is None:
                return "no CRS"
    except rasterio.errors.RasterioIOError as exc:
        return f"unreadable: {exc}"
    return None


def _redact(message: str, key: str) -> str:
    message = re.sub(r"(?i)(api_key=)[^&\s]+", r"\1[REDACTED]", message)
    return message.replace(key, "[REDACTED]") if key else message
