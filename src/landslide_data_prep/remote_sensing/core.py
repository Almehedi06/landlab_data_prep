"""Build HLS vegetation-index products for one AOI: config in, GeoTIFFs out.

Orchestration only. Change metrics live in indices.py, quality masking in
qa.py, reading and compositing in composite.py, catalog and credentials in
catalog.py. Composites are made on the native 30 m HLS grid and kept under
native/. Each product is then put on the analysis grid by
analysis_grid.align_to_grid, the same warp every other layer in the repo uses.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import shutil
import tempfile

import numpy as np
import rasterio

from landslide_data_prep.analysis_grid import Grid, align_to_grid, snap_grid
from landslide_data_prep.preflight import load_and_validate_aoi
from landslide_data_prep.remote_sensing import catalog
from landslide_data_prep.remote_sensing import indices as metrics
from landslide_data_prep.remote_sensing.composite import WindowComposite, composite_window
from landslide_data_prep.remote_sensing.config import RemoteSensingConfig

LOG = logging.getLogger(__name__)

MANIFEST_VERSION = "1.1"
MANIFEST_NAME = "remote_sensing_manifest.json"
REMOTE_SENSING_SUBDIR = "remote_sensing"
NATIVE_SUBDIR = "native"
NODATA = -9999.0


class EmptyWindowError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProductSpec:
    key: str  # file stem, e.g. hls_ndvi_pre
    field_name: str  # double-underscore name for grid models, e.g. hls__ndvi_pre
    units: str
    description: str
    is_count: bool = False  # counts are aligned with nearest, never interpolated


@dataclass(frozen=True)
class Product:
    spec: ProductSpec
    path: Path  # on the analysis grid
    native_path: Path  # on the native 30 m HLS grid

    @property
    def key(self) -> str:
        return self.spec.key


def product_specs(rs_cfg: RemoteSensingConfig) -> list[ProductSpec]:
    """The single definition of which products a config yields and what they are called."""
    specs: list[ProductSpec] = []
    for name in rs_cfg.indices:
        low = name.lower()
        for w in rs_cfg.windows:
            specs.append(
                ProductSpec(
                    f"hls_{low}_{w.name}",
                    f"hls__{low}_{w.name}",
                    "unitless",
                    f"Median {name} of clear HLS observations, {w.start} to {w.end}",
                )
            )
    for name in rs_cfg.indices:
        low = name.lower()
        if name == "NBR":
            specs.append(ProductSpec("hls_dnbr", "hls__dnbr", "unitless x1000", "dNBR = 1000 * (NBR_pre - NBR_post)"))
            specs.append(
                ProductSpec(
                    "hls_rdnbr",
                    "hls__rdnbr",
                    "unitless x1000",
                    "RdNBR = dNBR / sqrt(max(|NBR_pre|, 0.001))",
                )
            )
        else:
            specs.append(ProductSpec(f"hls_d{low}", f"hls__d{low}", "unitless", f"{name}_pre - {name}_post"))
    for w in rs_cfg.windows:
        specs.append(
            ProductSpec(
                f"hls_clear_count_{w.name}",
                f"hls__clear_count_{w.name}",
                "days",
                f"Clear solar days contributing to the {w.name} composite",
                is_count=True,
            )
        )
    return specs


def build_hls_products(
    rs_cfg: RemoteSensingConfig,
    aoi_path: str | Path,
    output_dir: str | Path,
    grid: Grid,
    *,
    continuous_resampling: str,
    max_workers: int = 8,
) -> list[Product]:
    output_dir = Path(output_dir)
    native_dir = output_dir / NATIVE_SUBDIR

    aoi = load_and_validate_aoi(aoi_path)
    native = snap_grid(grid.bounds, grid.crs, catalog.NATIVE_RESOLUTION_M)
    west, south, east, north = (float(v) for v in aoi.to_crs(epsg=4326).total_bounds)
    longitude = (west + east) / 2.0
    LOG.info(
        "HLS native grid %dx%d at %g m; analysis grid %dx%d at %g m in %s",
        native.width, native.height, native.resolution, grid.width, grid.height, grid.resolution, grid.crs,
    )

    # Catalog search is public and cheap. Run it for both windows first so an
    # empty window fails before credentials are checked or anything downloads.
    scenes_by_window: dict[str, list[catalog.Scene]] = {}
    stats_by_window: dict[str, dict] = {}
    for w in rs_cfg.windows:
        items = catalog.search_hls_vi((west, south, east, north), w.start, w.end)
        scenes, stats = catalog.scenes_from_items(items, rs_cfg.indices, rs_cfg.cloud_max)
        LOG.info("Window %s %s..%s: %s", w.name, w.start, w.end, stats)
        if not scenes:
            raise EmptyWindowError(
                f"Window '{w.name}' ({w.start} to {w.end}) has no usable HLS scenes: "
                f"{stats['items_found']} found, {stats['dropped_above_cloud_max']} above "
                f"cloud_max={rs_cfg.cloud_max:g}. Widen the window or raise cloud_max."
            )
        scenes_by_window[w.name] = scenes
        stats_by_window[w.name] = stats

    all_scenes = [s for scenes in scenes_by_window.values() for s in scenes]
    gdal_options = catalog.earthdata_gdal_options() if catalog.needs_earthdata_auth(all_scenes) else {}

    composites: dict[str, WindowComposite] = {}
    cookie_dir = Path(tempfile.mkdtemp(prefix="hls_cookies_"))
    try:
        factory = catalog.per_thread_options_factory(gdal_options, cookie_dir)
        for w in rs_cfg.windows:
            LOG.info("Compositing %s: %d scenes", w.name, len(scenes_by_window[w.name]))
            comp = composite_window(
                scenes_by_window[w.name], rs_cfg.indices, native, longitude, factory, max_workers=max_workers
            )
            if not np.any(comp.clear_count > 0):
                raise EmptyWindowError(
                    f"Window '{w.name}' ({w.start} to {w.end}): {comp.n_scenes} scenes read, but no "
                    "pixel had a clear observation. Widen the window."
                )
            composites[w.name] = comp
    finally:
        shutil.rmtree(cookie_dir, ignore_errors=True)

    arrays = _product_arrays(rs_cfg, composites)
    specs = product_specs(rs_cfg)
    drift = {s.key for s in specs} ^ set(arrays)
    if drift:  # product_specs and _product_arrays must name the same products
        raise AssertionError(f"Product definitions disagree: {sorted(drift)}")

    # Created only now, so a run that fails on search, credentials or reading leaves nothing behind.
    native_dir.mkdir(parents=True, exist_ok=True)
    products: list[Product] = []
    for spec in specs:
        tags = _tags(spec, rs_cfg)
        native_path = native_dir / f"{spec.key}.tif"
        _write_geotiff(native_path, arrays[spec.key], native, tags)
        path = align_to_grid(
            native_path,
            output_dir / f"{spec.key}.tif",
            grid,
            "nearest" if spec.is_count else continuous_resampling,
            aoi_path=aoi_path,
            tags=tags,
        )
        products.append(Product(spec=spec, path=path, native_path=native_path))

    manifest = _manifest(
        rs_cfg, aoi_path, output_dir, grid, native, continuous_resampling,
        scenes_by_window, stats_by_window, composites, products, arrays,
    )
    manifest_path = output_dir / MANIFEST_NAME
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True))
    for w in rs_cfg.windows:
        empty = manifest["windows"][w.name]["fraction_pixels_without_clear_obs"]
        if empty > 0.25:
            LOG.warning("Window %s: %.0f%% of pixels have no clear observation", w.name, 100 * empty)
    LOG.info("Wrote %d HLS products and %s", len(products), manifest_path)
    return products


def _product_arrays(
    rs_cfg: RemoteSensingConfig, composites: dict[str, WindowComposite]
) -> dict[str, np.ndarray]:
    arrays: dict[str, np.ndarray] = {}
    pre, post = composites["pre"], composites["post"]
    for name in rs_cfg.indices:
        low = name.lower()
        arrays[f"hls_{low}_pre"] = pre.medians[name]
        arrays[f"hls_{low}_post"] = post.medians[name]
    for name in rs_cfg.indices:
        low = name.lower()
        if name == "NBR":
            arrays["hls_dnbr"] = metrics.dnbr(pre.medians[name], post.medians[name])
            arrays["hls_rdnbr"] = metrics.rdnbr(pre.medians[name], post.medians[name])
        else:
            arrays[f"hls_d{low}"] = metrics.difference(pre.medians[name], post.medians[name])
    for w in rs_cfg.windows:
        arrays[f"hls_clear_count_{w.name}"] = composites[w.name].clear_count.astype(np.float32)
    return arrays


def _write_geotiff(path: Path, array: np.ndarray, grid: Grid, tags: dict[str, str]) -> None:
    data = np.where(np.isfinite(array), array, NODATA).astype(np.float32)
    with rasterio.open(path, "w", **grid.profile(nodata=NODATA)) as dst:
        dst.write(data, 1)
        dst.update_tags(**tags)


def _tags(spec: ProductSpec, rs_cfg: RemoteSensingConfig) -> dict[str, str]:
    return {
        "product": spec.key,
        "units": spec.units,
        "description": spec.description,
        "source": "NASA HLS vegetation indices v2.0 (" + ", ".join(catalog.HLS_VI_COLLECTIONS) + ")",
        "native_resolution_m": f"{catalog.NATIVE_RESOLUTION_M:g}",
        "window_pre": f"{rs_cfg.pre.start}/{rs_cfg.pre.end}",
        "window_post": f"{rs_cfg.post.start}/{rs_cfg.post.end}",
        "cloud_max": f"{rs_cfg.cloud_max:g}",
        "compositing": rs_cfg.compositing,
    }


def _manifest(
    rs_cfg, aoi_path, output_dir, grid, native, continuous_resampling,
    scenes_by_window, stats_by_window, composites, products, arrays,
) -> dict:
    windows = {}
    for w in rs_cfg.windows:
        comp = composites[w.name]
        scenes = scenes_by_window[w.name]
        count = comp.clear_count
        windows[w.name] = {
            "start": w.start.isoformat(),
            "end": w.end.isoformat(),
            **stats_by_window[w.name],
            "solar_days": len(comp.days),
            "first_scene_utc": scenes[0].datetime.isoformat(),
            "last_scene_utc": scenes[-1].datetime.isoformat(),
            "scene_ids": [s.id for s in scenes],
            "clear_days_min": int(count.min()),
            "clear_days_median": float(np.median(count)),
            "clear_days_max": int(count.max()),
            "fraction_pixels_without_clear_obs": float(np.mean(count == 0)),
        }
    return {
        "manifest_version": MANIFEST_VERSION,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "aoi": str(aoi_path),
        "output_dir": str(output_dir),
        "source": {"catalog": catalog.CMR_STAC_URL, "collections": list(catalog.HLS_VI_COLLECTIONS)},
        "native_resolution_m": catalog.NATIVE_RESOLUTION_M,
        "analysis_grid": grid.as_dict(),
        "native_grid": native.as_dict(),
        "resampling_to_analysis_grid": {"continuous": continuous_resampling, "counts": "nearest"},
        "config": {
            "windows": {w.name: {"start": w.start.isoformat(), "end": w.end.isoformat()} for w in rs_cfg.windows},
            "indices": list(rs_cfg.indices),
            "cloud_max": rs_cfg.cloud_max,
            "compositing": rs_cfg.compositing,
            "resampling": rs_cfg.resampling,
        },
        "windows": windows,
        "products": [
            {
                "key": p.spec.key,
                "field_name": p.spec.field_name,
                "file": p.path.name,
                "native_file": f"{NATIVE_SUBDIR}/{p.native_path.name}",
                "units": p.spec.units,
                "description": p.spec.description,
                "valid_fraction_native": float(np.mean(np.isfinite(arrays[p.spec.key]))),
            }
            for p in products
        ],
        "versions": {"rasterio": rasterio.__version__, "gdal": rasterio.__gdal_version__, "numpy": np.__version__},
    }
