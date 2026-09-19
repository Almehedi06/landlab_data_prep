"""Daily PRISM grids on the analysis grid: config in, forcing files out.

Each day and variable is downloaded once into the cache, keyed by PRISM's
release, so a revised grid is fetched again and an unchanged one is reused.
Every grid is aligned with the shared warp and written in the layout
landlab_debrisflow reads: aligned_tif/<variable>/, asc/<variable>/,
forcing_daily_prism.csv and prism_manifest.json.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime, timezone
import json
import logging
from pathlib import Path
import time
import zipfile

import numpy as np
import rasterio
import requests

from landlab_data_prep.analysis_grid import NODATA, Grid, align_to_grid
from landlab_data_prep.downloads import cached_download, extract_tifs
from landlab_data_prep.prism.config import PrismConfig
from landlab_data_prep.reproject_and_resample import convert_to_ascii

LOG = logging.getLogger(__name__)

BASE_URL = "https://services.nacse.org/prism/data/get"
REGION = "us"
PRISM_SUBDIR = "prism_forcing"
CSV_NAME = "forcing_daily_prism.csv"
MANIFEST_NAME = "prism_manifest.json"
POLITE_DELAY_SECONDS = 2.0  # PRISM asks clients not to hammer the service
FILE_PREFIX = {"ppt": "precip", "tmin": "tmin", "tmax": "tmax"}
CSV_COLUMN = {"ppt": "precip_mm", "tmin": "tmin_c", "tmax": "tmax_c"}
UNITS = {"ppt": "mm/day", "tmin": "degC", "tmax": "degC"}
# Nearest keeps each cell's modelled daily total; temperature varies smoothly.
RESAMPLING = {"ppt": "nearest", "tmin": "bilinear", "tmax": "bilinear"}


class PrismError(RuntimeError):
    pass


@dataclass(frozen=True)
class Release:
    date: str
    number: str

    @property
    def tag(self) -> str:
        return f"r{int(self.number):03d}_{self.date}"


def grid_url(resolution: str, variable: str, day: date) -> str:
    return f"{BASE_URL}/{REGION}/{resolution}/{variable}/{day:%Y%m%d}"


def release_url(resolution: str, variable: str, day: date) -> str:
    return f"{BASE_URL}/releaseDate/{REGION}/{resolution}/{variable}/{day:%Y%m%d}?json=true"


def parse_release(payload: object, label: str) -> Release:
    """PRISM answers with [day, release date, variable, release number, url]."""
    if isinstance(payload, list) and len(payload) >= 4:
        release_date, number = str(payload[1])[:10], str(payload[3])
        try:
            date.fromisoformat(release_date)
            if number.isdigit():
                return Release(release_date, number)
        except ValueError:
            pass
    raise PrismError(f"Unexpected PRISM release metadata for {label}: {payload!r}")


def fetch_release(resolution: str, variable: str, day: date, timeout: float = 60) -> Release:
    response = requests.get(release_url(resolution, variable, day), timeout=timeout)
    response.raise_for_status()
    return parse_release(response.json(), f"{variable} {day}")


def fetch_grid(resolution: str, variable: str, day: date, cache_dir: str | Path) -> tuple[Path, Release | None, bool]:
    """The cached zip for PRISM's current release, downloading it only when it is new.

    Returns (zip path, release or None when it could not be checked, downloaded).
    """
    folder = Path(cache_dir) / "prism" / resolution / variable
    stem = f"prism_{variable}_{REGION}_{resolution}_{day:%Y%m%d}"
    try:
        release = fetch_release(resolution, variable, day)
    except (requests.RequestException, ValueError, PrismError) as exc:
        cached = [p for p in sorted(folder.glob(f"{stem}_r*.zip")) if zipfile.is_zipfile(p)]
        if not cached:
            raise PrismError(f"Could not check the PRISM release for {variable} {day}, and nothing is cached: {exc}") from None
        LOG.warning("Could not check the PRISM release for %s %s (%s); using cached %s", variable, day, exc, cached[-1].name)
        return cached[-1], None, False

    name = f"{stem}_{release.tag}.zip"
    target = folder / name
    if zipfile.is_zipfile(target):
        return target, release, False
    target.unlink(missing_ok=True)  # a damaged earlier download
    time.sleep(POLITE_DELAY_SECONDS)
    try:
        cached_download(grid_url(resolution, variable, day), folder, filename=name)
    except RuntimeError as exc:
        raise PrismError(str(exc)) from None
    if not zipfile.is_zipfile(target):
        # PRISM answers some refusals, such as its repeat-download limit, with a message instead of a zip.
        message = target.read_bytes()[:300].decode(errors="replace").strip()
        target.unlink()
        raise PrismError(f"PRISM did not return a zip for {variable} {day}: {message!r}")
    return target, release, True


def _dataset_info(extract_dir: Path) -> dict[str, str]:
    wanted = ("PRISM_DATASET_TYPE", "PRISM_DATASET_VERSION", "PRISM_DATASET_RELEASE_NUMBER", "PRISM_DATASET_CREATE_DATE")
    info: dict[str, str] = {}
    for path in extract_dir.glob("*.info.txt"):
        for line in path.read_text(errors="replace").splitlines():
            key, sep, value = line.partition(":")
            if sep and key.strip() in wanted:
                info.setdefault(key.strip(), value.strip())
    return info


def build_prism_forcing(
    prism_cfg: PrismConfig,
    aoi_path: str | Path,
    output_dir: str | Path,
    grid: Grid,
    cache_dir: str | Path,
) -> Path:
    """Write daily grids, the forcing CSV and the manifest. Returns the CSV path."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    records: list[dict] = []

    for day in prism_cfg.days:
        row: dict = {"datetime": day.isoformat()}
        for variable in prism_cfg.variables:
            zip_path, release, downloaded = fetch_grid(prism_cfg.resolution, variable, day, cache_dir)
            extract_dir = zip_path.with_suffix("")
            tifs = extract_tifs(zip_path, extract_dir)
            if len(tifs) != 1:
                raise PrismError(f"Expected one GeoTIFF in {zip_path.name}, found {[t.name for t in tifs]}")

            name = f"{FILE_PREFIX[variable]}_{day:%Y%m%d}"
            tags = {
                "variable": variable,
                "units": UNITS[variable],
                "date": day.isoformat(),
                "source": grid_url(prism_cfg.resolution, variable, day),
                "release_date": release.date if release else "unverified",
                "release_number": release.number if release else "unverified",
            }
            try:
                aligned = align_to_grid(
                    tifs[0], output_dir / "aligned_tif" / variable / f"{name}.tif",
                    grid, RESAMPLING[variable], aoi_path=aoi_path, tags=tags,
                )
            except ValueError as exc:
                raise PrismError(f"PRISM {variable} {day}: {exc} PRISM covers the conterminous US only.") from None
            asc = Path(convert_to_ascii(str(aligned), str(output_dir / "asc" / variable), grid=grid))
            with rasterio.open(aligned) as src:
                values = src.read(1)
            aoi_mean = float(values[values != NODATA].mean())

            row[CSV_COLUMN[variable]] = aoi_mean
            row[f"{variable}_tif_path"] = aligned.relative_to(output_dir).as_posix()
            row[f"{variable}_asc_path"] = asc.relative_to(output_dir).as_posix()
            row[f"{variable}_release_date"] = release.date if release else ""
            row[f"{variable}_release_number"] = release.number if release else ""
            records.append({
                "date": day.isoformat(),
                "variable": variable,
                "aoi_mean": aoi_mean,
                "release_date": release.date if release else None,
                "release_number": release.number if release else None,
                "release_verified": release is not None,
                "downloaded": downloaded,
                "cache_file": zip_path.name,
                "dataset": _dataset_info(extract_dir),
            })
        rows.append(row)
        LOG.info("PRISM %s done", day)

    csv_path = output_dir / CSV_NAME
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    manifest = {
        "manifest_version": "1.0",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "start_date": prism_cfg.start.isoformat(),
        "end_date": prism_cfg.end.isoformat(),
        "variables": list(prism_cfg.variables),
        "units": {v: UNITS[v] for v in prism_cfg.variables},
        "resampling": {v: RESAMPLING[v] for v in prism_cfg.variables},
        "region": REGION,
        "resolution": prism_cfg.resolution,
        "source": BASE_URL,
        "aoi_path": str(aoi_path),
        "template": grid.as_dict(),
        "downloads": sum(r["downloaded"] for r in records),
        "reused_from_cache": sum(not r["downloaded"] for r in records),
        "unverified_releases": sum(not r["release_verified"] for r in records),
        "records": records,
        "versions": {"rasterio": rasterio.__version__, "gdal": rasterio.__gdal_version__, "numpy": np.__version__},
    }
    (output_dir / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2))
    LOG.info("PRISM: %d grids downloaded, %d reused from cache", manifest["downloads"], manifest["reused_from_cache"])
    return csv_path
