from __future__ import annotations

from dataclasses import dataclass
import logging
import os
from typing import Callable

import numpy as np

from landlab_data_prep.config import validate_config
from landlab_data_prep.dem import fetch_dem
from landlab_data_prep.downloads import cached_download, extract_first_tif, extract_tif_by_suffix
from landlab_data_prep.landlab_io import add_ascii_field, load_grid, read_nodata_value, write_ascii_field
from landlab_data_prep.preflight import (
    ensure_output_dir_writable,
    load_and_validate_aoi,
    validate_aoi_overlaps_raster,
    validate_raster_path,
)
from landlab_data_prep.analysis_grid import ALIGNED_SUBDIR, DEM_RESAMPLING, NODATA, Grid, align_to_grid, grid_from_config
from landlab_data_prep.reproject_and_resample import convert_to_ascii
from landlab_data_prep.soil_features import (
    compute_fc_wp_arrays,
    compute_ksat,
    compute_saturated_water_content,
    compute_soil_density,
    compute_soil_texture,
    compute_transmissivity,
)
from landlab_data_prep.vegetation_features import (
    adjust_internal_friction_angle,
    compute_cohesion,
    rootcohesion,
    vegtype,
)


LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class SourceSpec:
    key: str
    uri: str | list[str]
    resampling: str
    unzip: bool = False
    tif_suffix: str | None = None


@dataclass(frozen=True)
class FieldSpec:
    source_key: str
    field_name: str
    scale: float = 1.0
    offset: float = 0.0
    close_nodata: bool = True
    extra_close_values: list[float] | None = None
    rename_file: bool = True
    transform: Callable | None = None


def validate_pipeline_inputs(cfg: dict) -> None:
    """Everything checkable before a download: config structure, AOI, grid and local files."""
    validate_config(cfg, "pipeline")
    aoi_path = cfg["aoi"]["aoi"]
    load_and_validate_aoi(aoi_path)
    ensure_output_dir_writable(cfg["paths"]["output_dir"])
    grid_from_config(cfg)

    dem_cfg = cfg["dem"]
    if dem_cfg["source"] == "local":
        validate_raster_path(dem_cfg["path"], label="DEM")
        validate_aoi_overlaps_raster(aoi_path, dem_cfg["path"], label="DEM")

    for spec in build_sources_from_config(cfg):
        if isinstance(spec.uri, list):
            continue
        uri = spec.uri
        if uri.startswith("http"):
            continue
        local_path = uri
        if uri.lower().endswith(".zip"):
            if not os.path.exists(local_path):
                raise FileNotFoundError(f"Local source zip not found: {local_path}")
            continue
        validate_raster_path(local_path, label=f"Source {spec.key}")
        validate_aoi_overlaps_raster(aoi_path, local_path, label=f"Source {spec.key}")


def build_sources_from_config(cfg: dict) -> list[SourceSpec]:
    sources: list[SourceSpec] = []

    for key, info in cfg.get("feature_sources", {}).get("rasters", {}).items():
        sources.append(
            SourceSpec(
                key=key,
                uri=info["url"],
                resampling=info["resampling"],
            )
        )

    for key, info in cfg.get("feature_sources", {}).get("landcover", {}).items():
        sources.append(
            SourceSpec(
                key=key,
                uri=info["url"],
                resampling=info["resampling"],
                unzip=info["unzip"],
            )
        )

    bs = cfg["burn_severity"]
    src = bs["source"]
    local_path = os.path.join(bs["local"]["path"], bs["local"]["filename"]) if "local" in bs else None

    if src == "local":
        sources.append(
            SourceSpec(
                key="burn_severity",
                uri=local_path,
                resampling=bs["local"]["resampling"],
            )
        )
    elif src in {"remote", "remote_then_local", "auto"}:
        base_url = bs["remote"]["base_url"]
        fire_name_fmt = cfg["fire"]["name"].lower().replace(" ", "_")
        fire_id_fmt = cfg["fire"]["id"].lower()
        remote_candidates = [
            pattern.format(
                base_url=base_url,
                fire_name_fmt=fire_name_fmt,
                fire_id_fmt=fire_id_fmt,
            )
            for pattern in bs["remote"]["candidates"]
        ]
        all_candidates = (
            remote_candidates + [local_path]
            if src in {"remote_then_local", "auto"}
            else remote_candidates
        )
        sources.append(
            SourceSpec(
                key="burn_severity",
                uri=all_candidates,
                resampling=bs["remote"]["resampling"],
                unzip=True,
            )
        )
    else:
        raise ValueError(
            "Unsupported burn_severity.source="
            f"{src!r}. Expected one of: local, remote, remote_then_local, auto."
        )

    dn = cfg.get("dnbr") or {}
    if dn.get("enabled") is True:
        dn_src = dn["source"]
        dn_local_path = os.path.join(dn["local"]["path"], dn["local"]["filename"]) if "local" in dn else None

        if dn_src == "local":
            sources.append(
                SourceSpec(
                    key="dnbr",
                    uri=dn_local_path,
                    resampling=dn["local"]["resampling"],
                )
            )
        elif dn_src in {"remote", "remote_then_local", "auto"}:
            base_url = dn["remote"]["base_url"]
            fire_name_fmt = cfg["fire"]["name"].lower().replace(" ", "_")
            fire_id_fmt = cfg["fire"]["id"].lower()
            post_date_fmt = cfg["fire"].get("post_image_date", "")
            if not post_date_fmt:
                raise ValueError(
                    "dnbr.source is remote/auto but fire.post_image_date is not set "
                    "(needed to build the BAER preliminary-data filename, e.g. "
                    "'20240829' from the fire's BAER metadata)."
                )
            dn_remote_candidates = [
                pattern.format(
                    base_url=base_url,
                    fire_name_fmt=fire_name_fmt,
                    fire_id_fmt=fire_id_fmt,
                    post_date_fmt=post_date_fmt,
                )
                for pattern in dn["remote"]["candidates"]
            ]
            dn_all_candidates = (
                dn_remote_candidates + [dn_local_path]
                if dn_src in {"remote_then_local", "auto"}
                else dn_remote_candidates
            )
            sources.append(
                SourceSpec(
                    key="dnbr",
                    uri=dn_all_candidates,
                    resampling=dn["remote"]["resampling"],
                    unzip=True,
                    tif_suffix="_dnbr.tif",
                )
            )
        else:
            raise ValueError(
                "Unsupported dnbr.source="
                f"{dn_src!r}. Expected one of: local, remote, remote_then_local, auto."
            )

    return sources


def _resolve_single_source(spec: SourceSpec, cache_dir: str | None, work_dir: str) -> str:
    uri = spec.uri
    if isinstance(uri, list):
        raise TypeError("Expected a single URI, got list")

    if uri.startswith(("http://", "https://")):
        if not cache_dir:
            raise ValueError(f"{spec.key}: {uri} downloads, which needs paths.cache_dir in the config")
        local = cached_download(uri, cache_dir)
        if local.suffix.lower() != ".zip" and not spec.unzip:
            return str(local)
        return str(_tif_from_zip(local, os.path.join(cache_dir, f"{local.stem}_extracted"), spec))

    if not os.path.exists(uri):
        raise FileNotFoundError(f"Source not found: {uri}")
    if uri.lower().endswith(".zip"):
        return str(_tif_from_zip(uri, os.path.join(work_dir, f"unzipped_{spec.key}"), spec))
    return uri


def _tif_from_zip(zip_path, dest_dir: str, spec: SourceSpec):
    if spec.tif_suffix:
        return extract_tif_by_suffix(zip_path, dest_dir, spec.tif_suffix)
    return extract_first_tif(zip_path, dest_dir)


def resolve_source_to_tif(spec: SourceSpec, cache_dir: str | None, work_dir: str) -> str:
    """A local file or a cached download; for a candidate list, the first one that works."""
    if not isinstance(spec.uri, list):
        return _resolve_single_source(spec, cache_dir, work_dir)
    errors: list[str] = []
    for candidate in spec.uri:
        try:
            return _resolve_single_source(
                SourceSpec(
                    key=spec.key,
                    uri=candidate,
                    resampling=spec.resampling,
                    unzip=spec.unzip,
                    tif_suffix=spec.tif_suffix,
                ),
                cache_dir,
                work_dir,
            )
        except Exception as exc:
            errors.append(f"{candidate}: {exc}")
    raise RuntimeError(f"No candidate worked for {spec.key}:\n" + "\n".join(f"    {e}" for e in errors))


DOWNLOADS_SUBDIR = "_downloads"


def process_dem(dem_path: str, aoi_path: str, grid: Grid, output_dir: str) -> str:
    """Warp the DEM straight onto the analysis grid: one cubic resampling, no second pass."""
    aligned = align_to_grid(
        dem_path,
        os.path.join(output_dir, ALIGNED_SUBDIR, "dem.tif"),
        grid,
        DEM_RESAMPLING,
        aoi_path=aoi_path,
    )
    return convert_to_ascii(str(aligned), output_dir, grid=grid)


def _cleanup_intermediates(output_dir: str) -> None:
    """Remove downloads and aligned intermediates. Never touches other files in output_dir."""
    import shutil

    for name in (DOWNLOADS_SUBDIR, ALIGNED_SUBDIR):
        shutil.rmtree(os.path.join(output_dir, name), ignore_errors=True)


def process_source(
    spec: SourceSpec,
    aoi_path: str,
    grid: Grid,
    output_dir: str,
    cache_dir: str | None = None,
    cleanup_intermediates: bool = True,
) -> str:
    src_path = resolve_source_to_tif(spec, cache_dir, os.path.join(output_dir, DOWNLOADS_SUBDIR))
    aligned = align_to_grid(
        src_path,
        os.path.join(output_dir, ALIGNED_SUBDIR, f"{spec.key}.tif"),
        grid,
        spec.resampling,
        aoi_path=aoi_path,
    )
    ascii_path = convert_to_ascii(str(aligned), output_dir, grid=grid)

    if cleanup_intermediates:
        _cleanup_intermediates(output_dir)

    return ascii_path


def run_raster_pipeline(cfg: dict, cleanup_intermediates: bool = True) -> dict:
    validate_pipeline_inputs(cfg)
    grid = grid_from_config(cfg)
    aoi_path = cfg["aoi"]["aoi"]
    output_dir = cfg["paths"]["output_dir"]
    os.makedirs(output_dir, exist_ok=True)
    LOG.info("Analysis grid: %s", grid.as_dict())
    if "remote_sensing" in cfg:
        LOG.info("The remote_sensing block is built separately: landlab-prep-hls")

    cache_dir = cfg["paths"].get("cache_dir")
    dem_path = fetch_dem(aoi_path, cfg["dem"], cache_dir)
    outputs = {"dem": process_dem(str(dem_path), aoi_path, grid, output_dir)}

    # Every configured source is required. Collect all failures so one run shows them all.
    failures: list[str] = []
    for spec in build_sources_from_config(cfg):
        try:
            outputs[spec.key] = process_source(
                spec, aoi_path, grid, output_dir, cache_dir=cache_dir, cleanup_intermediates=cleanup_intermediates
            )
        except Exception as exc:
            failures.append(f"{spec.key}: {type(exc).__name__}: {exc}")
    if failures:
        raise RuntimeError(
            "Could not prepare every configured source:\n" + "\n".join(f"  - {f}" for f in failures)
        )

    if cleanup_intermediates:
        _cleanup_intermediates(output_dir)
    return outputs


def _burn_transform(values: np.ndarray) -> np.ndarray:
    values = np.asarray(values).copy()
    values[~np.isin(values, [2, 3, 4])] = 1
    return values


def _write_field(grid, output_dir: str, name: str) -> None:
    """Write a node field as ESRI ASCII, refusing values no reader can parse."""
    bad = int(np.count_nonzero(~np.isfinite(np.asarray(grid.at_node[name], dtype=float))))
    if bad:
        raise ValueError(f"{name} has {bad} non-finite values; refusing to write {name}.asc")
    write_ascii_field(os.path.join(output_dir, f"{name}.asc"), grid, name)


def run_landlab_pipeline(cfg: dict, outputs: dict, strict: bool = True):
    output_dir = cfg["paths"]["output_dir"]

    if "dem" not in outputs:
        raise ValueError("Missing DEM output; cannot build master grid.")

    grid = load_grid(outputs["dem"], "topographic__elevation")
    nodata_val = read_nodata_value(outputs["dem"])
    grid.set_nodata_nodes_to_closed(grid.at_node["topographic__elevation"], nodata_val)
    os.rename(outputs["dem"], os.path.join(output_dir, "topographic__elevation.asc"))

    field_map = {
        "cec7_0_cm": FieldSpec("cec7_0_cm", "cation__exchange_capacity", scale=0.1),
        "anylithicdpt_cm": FieldSpec("anylithicdpt_cm", "soil__thickness", scale=0.01),
        "claytotal_0_cm": FieldSpec("claytotal_0_cm", "clay__total"),
        "ph1to1h2o_0_cm": FieldSpec("ph1to1h2o_0_cm", "pH", scale=0.01),
        "sandtotal_0_cm": FieldSpec("sandtotal_0_cm", "sand__total"),
        "silttotal_0_cm": FieldSpec("silttotal_0_cm", "silt__total"),
        "dbovendry_0_cm": FieldSpec("dbovendry_0_cm", "dry__bulk_density", scale=0.01),
        "burn_severity": FieldSpec(
            "burn_severity",
            "burn__severity",
            close_nodata=False,
            transform=_burn_transform,
        ),
    }

    if (cfg.get("dnbr") or {}).get("enabled") is True:
        field_map["dnbr"] = FieldSpec("dnbr", "burn__dnbr")

    for source_key, spec in field_map.items():
        if source_key not in outputs:
            if strict:
                raise ValueError(f"Missing required source: {source_key}")
            LOG.warning("Skipping missing source: %s", source_key)
            continue
        add_ascii_field(
            grid,
            outputs[source_key],
            spec.field_name,
            scale=spec.scale,
            offset=spec.offset,
            close_nodata=spec.close_nodata,
            extra_close_values=spec.extra_close_values,
            rename_file=spec.rename_file,
            transform=spec.transform,
        )
        # Persist canonical values from the grid so scaled/transformed fields
        # (e.g., soil__thickness from cm -> m) are reflected in output ASCII.
        _write_field(grid, output_dir, spec.field_name)

    landcover_keys = list(cfg.get("feature_sources", {}).get("landcover", {}).keys())
    for key in landcover_keys:
        if key in outputs:
            add_ascii_field(
                grid,
                outputs[key],
                "landcover",
                extra_close_values=[11],
            )
            break
    else:
        if strict and landcover_keys:
            raise ValueError("Missing landcover source output.")

    required = [
        "pH",
        "clay__total",
        "silt__total",
        "cation__exchange_capacity",
        "soil__thickness",
        "dry__bulk_density",
        "sand__total",
    ]
    for name in required:
        if name not in grid.at_node:
            raise ValueError(f"Missing required field: {name}")

    # A cell missing any soil input is nodata in every soil-derived field. The
    # formulas would otherwise turn -9999 inputs into huge or infinite numbers.
    soil_missing = np.zeros(grid.number_of_nodes, dtype=bool)
    for name in required:
        soil_missing |= grid.at_node[name] == NODATA

    def add_soil_field(name: str, values) -> None:
        masked = np.where(soil_missing, NODATA, np.asarray(values, dtype=float))
        grid.add_field(name, masked, at="node", clobber=True)

    with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
        ksat = compute_ksat(
            grid.at_node["pH"],
            grid.at_node["clay__total"],
            grid.at_node["silt__total"],
            grid.at_node["cation__exchange_capacity"],
        )
        add_soil_field("soil__saturated_hydraulic_conductivity", (ksat / 100) * 10)
        add_soil_field(
            "soil__transmissivity",
            compute_transmissivity(
                grid.at_node["soil__saturated_hydraulic_conductivity"],
                grid.at_node["soil__thickness"],
            ),
        )
        add_soil_field(
            "saturated__water_content",
            compute_saturated_water_content(
                grid.at_node["dry__bulk_density"],
                grid.at_node["clay__total"],
                grid.at_node["silt__total"],
            ),
        )
        add_soil_field(
            "soil__texture",
            compute_soil_texture(
                grid.at_node["sand__total"],
                grid.at_node["silt__total"],
                grid.at_node["clay__total"],
            ),
        )
        porosity, theta_fc, theta_wp, phi = compute_fc_wp_arrays(
            grid.at_node["soil__texture"],
            grid.at_node["saturated__water_content"],
        )
        if "landcover" in grid.at_node:
            unclassified = phi == NODATA
            phi = adjust_internal_friction_angle(grid.at_node["landcover"], phi)
            phi[unclassified] = NODATA
        add_soil_field("field__capacity", theta_fc)
        add_soil_field("wilting__point", theta_wp)
        add_soil_field("porosity", porosity)
        add_soil_field("soil__internal_friction_angle", phi)
        density = compute_soil_density(grid.at_node["dry__bulk_density"], grid.at_node["porosity"])
        add_soil_field("soil__density", np.where(grid.at_node["porosity"] == NODATA, NODATA, density))

    for name in (
        "soil__saturated_hydraulic_conductivity",
        "soil__transmissivity",
        "saturated__water_content",
        "soil__texture",
        "field__capacity",
        "wilting__point",
        "porosity",
        "soil__internal_friction_angle",
        "soil__density",
    ):
        _write_field(grid, output_dir, name)

    if "landcover" in grid.at_node:
        landcover = grid.at_node["landcover"]
        landcover_color = rootcohesion(landcover, 0, 1, 2, 3, 4, 5)
        grid.add_field("landcovercolor", landcover_color, at="node", clobber=True)

        c_min, c_mode, c_max = compute_cohesion(landcover)
        grid.add_field("soil__minimum_total_cohesion", c_min, at="node", clobber=True)
        grid.add_field("soil__maximum_total_cohesion", c_max, at="node", clobber=True)
        grid.add_field("soil__mode_total_cohesion", c_mode, at="node", clobber=True)

        vegetation_type = vegtype(landcover, NODATA, 3, 2, 1, 0)
        grid.add_field("vegetation__plant_functional_type", vegetation_type, at="node", clobber=True)

        for name in (
            "soil__minimum_total_cohesion",
            "soil__maximum_total_cohesion",
            "soil__mode_total_cohesion",
            "vegetation__plant_functional_type",
        ):
            _write_field(grid, output_dir, name)

    return grid


def run_pipeline(config_path: str, cleanup_intermediates: bool = True):
    cfg = load_config(config_path)
    outputs = run_raster_pipeline(cfg, cleanup_intermediates=cleanup_intermediates)
    grid = run_landlab_pipeline(cfg, outputs)
    return outputs, grid
