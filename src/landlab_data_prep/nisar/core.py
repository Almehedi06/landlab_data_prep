"""NISAR layers on the analysis grid: config in, aligned layers out.

Two products: GCOV radar backscatter and SME2 soil moisture. For each scene over
the AOI only a window around the grid is read from ASF, and kept in the cache at
NISAR's own pixels and values, with every pixel NISAR marks invalid set to NoData
before anything is resampled. The shared warp then puts each layer on the
analysis grid. Each product writes to nisar_<product>/: aligned_tif/<layer>/,
asc/<layer>/, nisar_<product>_index.csv and nisar_manifest.json.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
import csv
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import logging
from pathlib import Path

import h5py
import numpy as np
import rasterio
from rasterio.transform import Affine
from rasterio.warp import transform_bounds

from landlab_data_prep.analysis_grid import NODATA, Grid, aoi_mask, align_to_grid, source_window
from landlab_data_prep.earthdata import RemoteFile, earthdata_session
from landlab_data_prep.nisar.catalog import COLLECTIONS, NisarError, Scene, search_scenes
from landlab_data_prep.nisar.config import PRODUCT_LAYERS, NisarConfig
from landlab_data_prep.reproject_and_resample import convert_to_ascii

LOG = logging.getLogger(__name__)

MANIFEST_NAME = "nisar_manifest.json"
# Native pixels read beyond the grid on every side, so edge cells see whole source pixels.
WINDOW_PAD = 2
SOIL_MOISTURE = "soil moisture, m3/m3"


@dataclass(frozen=True)
class Product:
    group: str  # HDF5 group holding the grid's coordinates and projection
    datasets: dict[str, str]  # config layer name -> dataset path under the group
    units: dict[str, str]
    resampling: str
    valid_pixels: str
    mask: str | None = None  # GCOV: dataset whose values 1-5 mark valid pixels


PRODUCTS = {
    "GCOV": Product(
        group="/science/LSAR/GCOV/grids/frequencyA",
        datasets={name: name for name in PRODUCT_LAYERS["GCOV"]},
        units={name: "gamma0 backscatter power, linear (RTC)" for name in PRODUCT_LAYERS["GCOV"]},
        # Area-weighted mean of linear power, the right average for backscatter at any cell size.
        resampling="average",
        valid_pixels="mask 1-5 and finite; mask 0 (invalid), mask 255 (outside the image) and NaN are NoData",
        mask="mask",
    ),
    "SME2": Product(
        group="/science/LSAR/SME2/grids",
        datasets={
            "combined": "soilMoisture",
            "DSG": "algorithmCandidates/DSG/soilMoisture",
            "PMI": "algorithmCandidates/PMI/soilMoisture",
            "TSR": "algorithmCandidates/TSR/soilMoisture",
            "recommended": "retrievalQualityFlag",
        },
        units={"combined": SOIL_MOISTURE, "DSG": SOIL_MOISTURE, "PMI": SOIL_MOISTURE, "TSR": SOIL_MOISTURE,
               "recommended": "1 where NISAR recommends the combined retrieval, 0 where it does not"},
        # 200 m cells are copied onto the grid, never smoothed into detail NISAR does not have.
        resampling="nearest",
        valid_pixels="_FillValue and non-finite values are NoData; recommended is 1 where bit 0 of "
                     "retrievalQualityFlag is 0",
    ),
}


@dataclass(frozen=True)
class NativeSubset:
    crs: str
    transform: Affine
    layers: dict[str, np.ndarray]  # float32, NODATA where NISAR has no valid value
    window: tuple[int, int, int, int]  # row_start, row_stop, col_start, col_stop in the scene
    absent: list[str] = field(default_factory=list)  # requested layers the scene does not carry


def _layer_values(dataset: h5py.Dataset, layer: str, rows: slice, cols: slice) -> np.ndarray:
    raw = dataset[rows, cols]
    invalid = ~np.isfinite(raw) if raw.dtype.kind == "f" else np.zeros(raw.shape, dtype=bool)
    fill = dataset.attrs.get("_FillValue")
    if fill is not None:
        invalid |= raw == np.asarray(fill).astype(raw.dtype)
    if layer == "recommended":
        values = ((raw.astype(np.int64) & 1) == 0).astype("float32")
    else:
        values = raw.astype("float32")
    values[invalid] = NODATA
    return values


def read_native_window(
    h5: h5py.File, product: str, layers: tuple[str, ...], bounds: tuple[float, float, float, float],
    bounds_crs: str,
) -> NativeSubset | None:
    """The scene's pixels around ``bounds``, values untouched, invalid pixels set to NODATA.

    Returns None when the scene's grid does not reach ``bounds``.
    """
    spec = PRODUCTS[product]
    if spec.group not in h5:  # e.g. a GCOV scene acquired without frequency A
        return NativeSubset("", Affine.identity(), {}, (0, 0, 0, 0), list(layers))
    group = h5[spec.group]
    epsg = int(group["projection"][()])
    try:
        window = source_window(group["xCoordinates"][()], group["yCoordinates"][()], f"EPSG:{epsg}",
                               bounds, bounds_crs, WINDOW_PAD)
    except ValueError as exc:
        raise NisarError(f"{product} scene: {exc}") from None
    if window is None:
        return None
    rows, cols, transform = window
    shape = (len(group["yCoordinates"]), len(group["xCoordinates"]))

    valid = None
    if spec.mask:
        mask = group[spec.mask][rows, cols]
        valid = (mask >= 1) & (mask <= 5)
    data, absent = {}, []
    for layer in layers:
        path = spec.datasets[layer]
        if path not in group:
            absent.append(layer)
            continue
        if group[path].shape != shape:
            raise NisarError(f"{product} {path} has shape {group[path].shape}, not the grid's {shape}")
        values = _layer_values(group[path], layer, rows, cols)
        if valid is not None:
            values[~valid] = NODATA
        data[layer] = values
    return NativeSubset(f"EPSG:{epsg}", transform, data, (rows.start, rows.stop, cols.start, cols.stop), absent)


def _write_native(path: Path, subset: NativeSubset, layer: str, scene: Scene, units: str) -> None:
    values = subset.layers[layer]
    profile = {
        "driver": "GTiff", "width": values.shape[1], "height": values.shape[0], "count": 1,
        "dtype": "float32", "crs": subset.crs, "transform": subset.transform, "nodata": NODATA,
        "compress": "deflate",
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(values, 1)
        dst.update_tags(source=scene.url, scene=scene.id, release=scene.release, layer=layer, units=units)


def fetch_subset(
    scene: Scene,
    product: str,
    layers: tuple[str, ...],
    grid: Grid,
    cache_dir: str | Path,
    open_scene: Callable[[Scene], RemoteFile],
    refresh: bool = False,
) -> tuple[dict[str, Path] | None, dict]:
    """Native-pixel GeoTIFFs of the scene's ``layers`` around the grid, from the cache or from ASF.

    Returns (layer -> path for the layers the scene carries, or None when the scene
    does not reach the grid; record).
    """
    folder = Path(cache_dir) / "nisar" / scene.id / grid.cache_key
    record_path = folder / "subset.json"
    paths = {layer: folder / f"{layer}.tif" for layer in layers}
    if record_path.is_file() and not refresh:
        record = json.loads(record_path.read_text())
        if not record["reaches_grid"]:
            return None, dict(record, downloaded=False)
        absent = set(record.get("absent_layers", []))
        if all(paths[layer].is_file() or layer in absent for layer in layers):
            return {k: v for k, v in paths.items() if k not in absent}, dict(record, downloaded=False)

    with open_scene(scene) as remote, h5py.File(remote, "r") as h5:
        subset = read_native_window(h5, product, layers, grid.bounds, grid.crs)
        fetched, calls, size = remote.bytes_fetched, remote.range_requests, remote.size

    folder.mkdir(parents=True, exist_ok=True)
    record = {
        "scene": scene.id, "release": scene.release, "url": scene.url, "reaches_grid": subset is not None,
        "file_bytes": size, "bytes_fetched": fetched, "range_requests": calls,
    }
    if subset is None:
        record_path.write_text(json.dumps(record, indent=2))
        return None, dict(record, downloaded=True)
    for layer in subset.layers:
        _write_native(paths[layer], subset, layer, scene, PRODUCTS[product].units[layer])
    record.update(
        crs=subset.crs, transform=list(subset.transform)[:6], window=list(subset.window),
        absent_layers=subset.absent,
        valid_pixels={layer: int((v != NODATA).sum()) for layer, v in subset.layers.items()},
    )
    record_path.write_text(json.dumps(record, indent=2))  # last, so a partial write is never reused
    return {layer: paths[layer] for layer in subset.layers}, dict(record, downloaded=True)


def _scene_opener() -> Callable[[Scene], RemoteFile]:
    """Credentials are looked up on the first read, so a fully cached run needs none."""
    state: dict = {}

    def open_scene(scene: Scene) -> RemoteFile:
        if not state:
            state["session"], state["headers"] = earthdata_session()
        return RemoteFile(scene.url, state["session"], state["headers"])

    return open_scene


def _build_product(
    product: str,
    layers: tuple[str, ...],
    nisar_cfg: NisarConfig,
    scenes: list[Scene],
    search: dict,
    aoi_path: str | Path,
    output_dir: Path,
    grid: Grid,
    cache_dir: str | Path,
    refresh: bool,
    open_scene: Callable[[Scene], RemoteFile],
) -> Path:
    spec = PRODUCTS[product]
    output_dir.mkdir(parents=True, exist_ok=True)
    aoi_cells = int(aoi_mask(aoi_path, grid).sum())
    beta = sum(s.release == "beta" for s in scenes)
    if beta:
        LOG.warning("NISAR %s: %d of %d scenes are Beta (pre-calibration) products", product, beta, len(scenes))

    rows: list[dict] = []
    records: list[dict] = []
    for scene in scenes:
        paths, record = fetch_subset(scene, product, layers, grid, cache_dir, open_scene, refresh)
        row: dict = {
            "datetime": scene.start.isoformat(), "scene": scene.id, "release": scene.release,
            "track": scene.track, "direction": scene.direction, "frame": scene.frame, "note": "",
        }
        for layer in layers:
            row.update({f"{layer}_coverage": 0.0, f"{layer}_mean": "", f"{layer}_tif_path": "",
                        f"{layer}_asc_path": ""})
        if paths is None:
            row["note"] = "scene does not reach the AOI"
        else:
            notes, empty = [], []
            absent = [layer for layer in layers if layer not in paths]
            if absent:
                notes.append(f"scene lacks {', '.join(absent)}")
            for layer, path in paths.items():
                target = output_dir / "aligned_tif" / layer / f"{layer}_{scene.stem}.tif"
                tags = {"layer": layer, "units": spec.units[layer], "scene": scene.id, "release": scene.release,
                        "source": scene.url, "datetime": scene.start.isoformat(), "direction": scene.direction}
                try:
                    aligned = align_to_grid(path, target, grid, spec.resampling, aoi_path=aoi_path, tags=tags)
                except ValueError as exc:
                    if "no valid pixels" not in str(exc):
                        raise
                    empty.append(layer)  # NISAR has no valid value in any AOI cell
                    continue
                asc = Path(convert_to_ascii(str(aligned), str(output_dir / "asc" / layer), grid=grid))
                with rasterio.open(aligned) as src:
                    values = src.read(1)
                valid = values != NODATA
                row[f"{layer}_coverage"] = float(valid.sum() / aoi_cells)
                row[f"{layer}_mean"] = float(values[valid].mean())
                row[f"{layer}_tif_path"] = aligned.relative_to(output_dir).as_posix()
                row[f"{layer}_asc_path"] = asc.relative_to(output_dir).as_posix()
            if empty:
                notes.append(f"no AOI data: {', '.join(empty)}")
            row["note"] = "; ".join(notes)
        rows.append(row)
        records.append(record)
        LOG.info("NISAR %s %s (%s): AOI coverage %s", product, scene.stem, scene.release,
                 ", ".join(f"{layer} {row[f'{layer}_coverage']:.2f}" for layer in layers))

    csv_path = output_dir / f"nisar_{product.lower()}_index.csv"
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    with_data = sum(any(r[f"{layer}_tif_path"] for layer in layers) for r in rows)
    manifest = {
        "manifest_version": "1.0",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "product": product,
        "collections": dict((short_name, release) for release, short_name in COLLECTIONS[product]),
        "releases_used": dict(Counter(s.release for s in scenes)),
        "layers": list(layers),
        "datasets": {layer: f"{spec.group}/{spec.datasets[layer]}" for layer in layers},
        "units": {layer: spec.units[layer] for layer in layers},
        "resampling": spec.resampling,
        "valid_pixels": spec.valid_pixels,
        "start_date": nisar_cfg.start.isoformat(),
        "end_date": nisar_cfg.end.isoformat(),
        "search": search,
        "aoi_path": str(aoi_path),
        "template": grid.as_dict(),
        "scenes": len(scenes),
        "scenes_with_data": with_data,
        "downloaded": sum(r["downloaded"] for r in records),
        "reused_from_cache": sum(not r["downloaded"] for r in records),
        "records": records,
        "versions": {
            "h5py": h5py.__version__, "hdf5": h5py.version.hdf5_version, "rasterio": rasterio.__version__,
            "gdal": rasterio.__gdal_version__, "numpy": np.__version__,
        },
    }
    (output_dir / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2))
    if not with_data:
        raise NisarError(f"None of the {len(scenes)} {product} scenes has valid pixels in the AOI; see {csv_path}")
    LOG.info("NISAR %s: %d of %d scenes have AOI data", product, with_data, len(scenes))
    return csv_path


def build_nisar_layers(
    nisar_cfg: NisarConfig,
    aoi_path: str | Path,
    output_root: str | Path,
    grid: Grid,
    cache_dir: str | Path,
    refresh: bool = False,
    open_scene: Callable[[Scene], RemoteFile] | None = None,
) -> list[Path]:
    """Every configured product into <output_root>/nisar_<product>/. Returns the index CSV paths."""
    bbox = transform_bounds(grid.crs, "EPSG:4326", *grid.bounds, densify_pts=21)
    found = {
        product: search_scenes(product, bbox, nisar_cfg.start, nisar_cfg.end) for product in nisar_cfg.layers
    }
    empty = [product for product, (scenes, _) in found.items() if not scenes]
    if empty:
        raise NisarError(
            f"No NISAR {' or '.join(empty)} scenes over the AOI from {nisar_cfg.start} to {nisar_cfg.end}. "
            "NISAR scenes start in October 2025; check coverage at https://search.asf.alaska.edu."
        )
    open_scene = open_scene or _scene_opener()
    paths = []
    for product, layers in nisar_cfg.layers.items():
        scenes, search = found[product]
        LOG.info("NISAR: %d %s scenes over the AOI", len(scenes), product)
        paths.append(_build_product(
            product, layers, nisar_cfg, scenes, search, aoi_path,
            Path(output_root) / f"nisar_{product.lower()}", grid, cache_dir, refresh, open_scene,
        ))
    return paths
