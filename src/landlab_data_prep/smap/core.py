"""SMAP L4 soil moisture on the analysis grid: config in, aligned layers out.

SMAP L4 (SPL4SMGP) gives surface (0-5 cm), root-zone (0-100 cm) and profile soil
moisture every 3 hours on the 9 km EASE-Grid 2.0. It comes from a land model
that assimilates SMAP observations, so it has values under snow and frozen
ground too. Each file stores a layer as one global compressed block, so reading
a layer costs about 4 MB instead of the whole 140 MB file. Only the window
around the grid is kept in the cache, at SMAP's own cells and values, and values
are copied onto the analysis grid by nearest cell. Output: aligned_tif/<layer>/,
smap_l4_index.csv (one row per 3-hour step), smap_l4_daily.csv (UTC days) and
smap_manifest.json.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
import csv
from dataclasses import dataclass
from datetime import date, datetime, timezone
import json
import logging
from pathlib import Path
import re
import threading

import h5py
import numpy as np
import rasterio
from rasterio.transform import Affine
from rasterio.warp import transform_bounds

from landlab_data_prep.analysis_grid import NODATA, Grid, aoi_mask, align_to_grid, source_window
from landlab_data_prep.earthdata import DATA_REL, RemoteFile, cmr_granules, earthdata_session
from landlab_data_prep.smap.config import SmapConfig

LOG = logging.getLogger(__name__)

SHORT_NAME, VERSION = "SPL4SMGP", "008"
SMAP_SUBDIR = "smap_l4"
INDEX_NAME, DAILY_NAME, MANIFEST_NAME = "smap_l4_index.csv", "smap_l4_daily.csv", "smap_manifest.json"
GROUP = "Geophysical_Data"
UNITS = "m3/m3"
EPSG = "EPSG:6933"  # EASE-Grid 2.0 global
# SMAP L4 files keep their metadata scattered, so small blocks read far fewer bytes.
BLOCK_SIZE = 256 * 1024
WORKERS = 4
# Native cells read beyond the grid on every side.
WINDOW_PAD = 1
_NAME = re.compile(r"SMAP_L4_SM_gph_(?P<time>\d{8}T\d{6})_V(?P<crid>\w+)_(?P<counter>\d{3})\.h5")


class SmapError(RuntimeError):
    pass


@dataclass(frozen=True)
class Granule:
    id: str  # the file name
    url: str
    time: datetime  # centre of the 3-hour average, UTC
    crid: str
    counter: str

    @property
    def stem(self) -> str:
        return f"{self.time:%Y%m%dT%H%M}"


def parse_granule(name: str, url: str) -> Granule:
    match = _NAME.fullmatch(name)
    if not match:
        raise SmapError(f"Unrecognised SMAP L4 file name {name!r}")
    time = datetime.strptime(match["time"], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
    return Granule(id=name, url=url, time=time, crid=match["crid"], counter=match["counter"])


def file_name(entry: dict) -> str:
    """The granule's file name. Reprocessed granules carry a suffix on their CMR title, not on the file."""
    return entry.get("producer_granule_id") or entry.get("title", "")


def granule_url(entry: dict) -> str:
    name = file_name(entry)
    urls = sorted({
        link["href"] for link in entry.get("links", [])
        if link.get("rel") == DATA_REL and link.get("href", "").startswith("https://")
        and link["href"].endswith(f"/{name}")
    })
    if len(urls) != 1:
        raise SmapError(f"Expected one HTTPS download link for {name!r}, found {urls}")
    return urls[0]


def search_granules(
    bbox_lonlat: tuple[float, float, float, float], start: date, end: date
) -> tuple[list[Granule], dict]:
    """One granule per 3-hour step whose centre falls on start..end (UTC days)."""
    params = {
        "short_name": SHORT_NAME, "version": VERSION,
        "bounding_box": ",".join(f"{v:.6f}" for v in bbox_lonlat),
        "temporal": f"{start.isoformat()}T00:00:00Z,{end.isoformat()}T23:59:59Z",
        "sort_key": "start_date",
    }
    entries = cmr_granules(params)
    best: dict[datetime, Granule] = {}
    for entry in entries:
        granule = parse_granule(file_name(entry), granule_url(entry))
        if not start <= granule.time.date() <= end:
            continue  # its 3-hour average belongs to the day before or after
        kept = best.get(granule.time)
        if kept is None or (granule.crid, granule.counter) > (kept.crid, kept.counter):
            best[granule.time] = granule
    granules = [best[t] for t in sorted(best)]
    return granules, {"collection": f"{SHORT_NAME} v{VERSION}", "query": params, "granules_found": len(entries)}


def read_window(
    h5: h5py.File, layers: tuple[str, ...], bounds: tuple[float, float, float, float], bounds_crs: str
) -> tuple[Affine, dict[str, np.ndarray]] | None:
    """SMAP's cells around ``bounds``, values untouched, fill set to NODATA. None if the grid misses."""
    mapping = h5["EASE2_global_projection"].attrs
    if _text(mapping.get("grid_mapping_name")) != "lambert_cylindrical_equal_area" or \
            float(mapping.get("standard_parallel", 0)) != 30.0:
        raise SmapError("SMAP L4 file is not on the global EASE-Grid 2.0")
    try:
        window = source_window(h5["x"][()], h5["y"][()], EPSG, bounds, bounds_crs, WINDOW_PAD)
    except ValueError as exc:
        raise SmapError(f"SMAP L4 file: {exc}") from None
    if window is None:
        return None
    rows, cols, transform = window
    data = {}
    for layer in layers:
        dataset = h5[GROUP][layer]
        values = dataset[rows, cols].astype("float32")
        fill = dataset.attrs.get("_FillValue")
        invalid = ~np.isfinite(values)
        if fill is not None:
            invalid |= values == np.float32(np.asarray(fill).item())
        values[invalid] = NODATA
        data[layer] = values
    return transform, data


def _text(value) -> str:
    if isinstance(value, np.ndarray):
        value = value.item() if value.size == 1 else value.tobytes()
    return value.decode() if isinstance(value, bytes) else str(value)


def fetch_window(
    granule: Granule, layers: tuple[str, ...], grid: Grid, cache_dir: str | Path,
    open_file: Callable[[Granule], RemoteFile], refresh: bool = False,
) -> tuple[dict[str, Path] | None, dict]:
    """Native-cell GeoTIFFs of ``layers`` around the grid, from the cache or from NSIDC."""
    folder = Path(cache_dir) / "smap" / f"{SHORT_NAME}_{VERSION}" / granule.id.removesuffix(".h5") / grid.cache_key
    record_path = folder / "window.json"
    paths = {layer: folder / f"{layer}.tif" for layer in layers}
    if record_path.is_file() and not refresh:
        record = json.loads(record_path.read_text())
        if not record["reaches_grid"]:
            return None, dict(record, downloaded=False)
        if all(p.is_file() for p in paths.values()):
            return paths, dict(record, downloaded=False)

    with open_file(granule) as remote, h5py.File(remote, "r") as h5:
        window = read_window(h5, layers, grid.bounds, grid.crs)
        fetched, size = remote.bytes_fetched, remote.size
    folder.mkdir(parents=True, exist_ok=True)
    record = {"granule": granule.id, "url": granule.url, "reaches_grid": window is not None,
              "file_bytes": size, "bytes_fetched": fetched}
    if window is not None:
        transform, data = window
        for layer, values in data.items():
            profile = {"driver": "GTiff", "width": values.shape[1], "height": values.shape[0], "count": 1,
                       "dtype": "float32", "crs": EPSG, "transform": transform, "nodata": NODATA,
                       "compress": "deflate"}
            with rasterio.open(paths[layer], "w", **profile) as dst:
                dst.write(values, 1)
                dst.update_tags(source=granule.url, layer=layer, units=UNITS, time=granule.time.isoformat())
        record["valid_cells"] = {layer: int((v != NODATA).sum()) for layer, v in data.items()}
    record_path.write_text(json.dumps(record, indent=2))  # last, so a partial write is never reused
    return (paths if window is not None else None), dict(record, downloaded=True)


def _file_opener() -> Callable[[Granule], RemoteFile]:
    """One session per worker thread; credentials are looked up on the first read."""
    local = threading.local()

    def open_file(granule: Granule) -> RemoteFile:
        if not hasattr(local, "session"):
            local.session, local.headers = earthdata_session()
        return RemoteFile(granule.url, local.session, local.headers, block_size=BLOCK_SIZE)

    return open_file


def _write_csv(path: Path, rows: list[dict]) -> None:
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def build_smap_layers(
    smap_cfg: SmapConfig,
    aoi_path: str | Path,
    output_dir: str | Path,
    grid: Grid,
    cache_dir: str | Path,
    refresh: bool = False,
    open_file: Callable[[Granule], RemoteFile] | None = None,
) -> Path:
    """Write the aligned layers, both CSVs and the manifest. Returns the index CSV path."""
    output_dir = Path(output_dir)
    layers = smap_cfg.layers
    bbox = transform_bounds(grid.crs, "EPSG:4326", *grid.bounds, densify_pts=21)
    granules, search = search_granules(bbox, smap_cfg.start, smap_cfg.end)
    if not granules:
        raise SmapError(f"No SMAP L4 files over the AOI from {smap_cfg.start} to {smap_cfg.end}.")
    LOG.info("SMAP: %d 3-hour files from %s to %s", len(granules), smap_cfg.start, smap_cfg.end)
    output_dir.mkdir(parents=True, exist_ok=True)
    open_file = open_file or _file_opener()
    aoi_cells = int(aoi_mask(aoi_path, grid).sum())

    with ThreadPoolExecutor(WORKERS) as pool:
        fetched = list(pool.map(
            lambda g: fetch_window(g, layers, grid, cache_dir, open_file, refresh), granules
        ))

    rows: list[dict] = []
    for granule, (paths, _) in zip(granules, fetched, strict=True):
        row: dict = {"datetime": granule.time.isoformat(), "granule": granule.id}
        empty = []
        for layer in layers:
            row.update({f"{layer}_coverage": 0.0, f"{layer}_mean": "", f"{layer}_tif_path": ""})
            if paths is None:
                continue
            target = output_dir / "aligned_tif" / layer / f"{layer}_{granule.stem}.tif"
            tags = {"layer": layer, "units": UNITS, "source": granule.url, "datetime": granule.time.isoformat()}
            try:
                aligned = align_to_grid(paths[layer], target, grid, "nearest", aoi_path=aoi_path, tags=tags)
            except ValueError as exc:
                if "no valid pixels" not in str(exc):
                    raise
                empty.append(layer)
                continue
            with rasterio.open(aligned) as src:
                values = src.read(1)
            valid = values != NODATA
            row[f"{layer}_coverage"] = float(valid.sum() / aoi_cells)
            row[f"{layer}_mean"] = float(values[valid].mean())
            row[f"{layer}_tif_path"] = aligned.relative_to(output_dir).as_posix()
        row["note"] = "file does not reach the AOI" if paths is None else (
            f"no AOI data: {', '.join(empty)}" if empty else "")
        rows.append(row)

    index_path = output_dir / INDEX_NAME
    _write_csv(index_path, rows)

    by_day: dict[str, list[dict]] = {}
    for row in rows:
        by_day.setdefault(row["datetime"][:10], []).append(row)
    daily = []
    for day, steps in by_day.items():
        entry: dict = {"date": day, "steps": len(steps)}
        for layer in layers:
            means = [r[f"{layer}_mean"] for r in steps if r[f"{layer}_mean"] != ""]
            entry[f"{layer}_mean"] = float(np.mean(means)) if means else ""
            entry[f"{layer}_steps_with_data"] = len(means)
        daily.append(entry)
    _write_csv(output_dir / DAILY_NAME, daily)

    records = [record for _, record in fetched]
    with_data = sum(any(r[f"{layer}_tif_path"] for layer in layers) for r in rows)
    manifest = {
        "manifest_version": "1.0",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "product": f"SMAP L4 {SHORT_NAME} v{VERSION}, 3-hourly, 9 km EASE-Grid 2.0",
        "layers": list(layers),
        "datasets": {layer: f"/{GROUP}/{layer}" for layer in layers},
        "units": UNITS,
        "resampling": "nearest",
        "valid_pixels": "_FillValue (-9999) and non-finite values are NoData",
        "time": "datetime is the centre of each 3-hour average, UTC; daily rows are UTC days",
        "start_date": smap_cfg.start.isoformat(),
        "end_date": smap_cfg.end.isoformat(),
        "search": search,
        "aoi_path": str(aoi_path),
        "template": grid.as_dict(),
        "files": len(granules),
        "files_with_data": with_data,
        "downloaded": sum(r["downloaded"] for r in records),
        "reused_from_cache": sum(not r["downloaded"] for r in records),
        "bytes_fetched": sum(r.get("bytes_fetched", 0) for r in records if r["downloaded"]),
        "records": records,
        "versions": {"h5py": h5py.__version__, "hdf5": h5py.version.hdf5_version, "rasterio": rasterio.__version__,
                     "gdal": rasterio.__gdal_version__, "numpy": np.__version__},
    }
    (output_dir / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2))
    if not with_data:
        raise SmapError(f"None of the {len(granules)} SMAP files has valid cells in the AOI; see {index_path}")
    LOG.info("SMAP: %d of %d files have AOI data; %d read, %d from cache", with_data, len(granules),
             manifest["downloaded"], manifest["reused_from_cache"])
    return index_path
