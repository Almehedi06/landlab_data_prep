from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil

import yaml

from analysis_grid import ALIGNED_SUBDIR, Grid, GridMismatchError, align_to_grid, check_on_grid
from preflight import (
    ensure_output_dir_writable,
    load_and_validate_aoi,
    validate_aoi_overlaps_raster,
    validate_raster_path,
)


MANIFEST_VERSION = "1.0"


@dataclass(frozen=True)
class SoilVarSpec:
    key: str
    field_name: str
    url: str
    resampling: str = "bilinear"


@dataclass(frozen=True)
class RasterSourceSpec:
    key: str
    uri: str | list[str]
    resampling: str
    unzip: bool = False


DEFAULT_SOIL_SPECS: dict[str, SoilVarSpec] = {
    "cec7_0_cm": SoilVarSpec(
        key="cec7_0_cm",
        field_name="cation__exchange_capacity",
        url="https://storage.googleapis.com/solus100pub/cec7_0_cm_p.tif",
        resampling="bilinear",
    ),
    "anylithicdpt_cm": SoilVarSpec(
        key="anylithicdpt_cm",
        field_name="soil__thickness",
        url="https://storage.googleapis.com/solus100pub/anylithicdpt_cm_p.tif",
        resampling="bilinear",
    ),
    "claytotal_0_cm": SoilVarSpec(
        key="claytotal_0_cm",
        field_name="clay__total",
        url="https://storage.googleapis.com/solus100pub/claytotal_0_cm_p.tif",
        resampling="bilinear",
    ),
    "ph1to1h2o_0_cm": SoilVarSpec(
        key="ph1to1h2o_0_cm",
        field_name="pH",
        url="https://storage.googleapis.com/solus100pub/ph1to1h2o_0_cm_p.tif",
        resampling="bilinear",
    ),
    "sandtotal_0_cm": SoilVarSpec(
        key="sandtotal_0_cm",
        field_name="sand__total",
        url="https://storage.googleapis.com/solus100pub/sandtotal_0_cm_p.tif",
        resampling="bilinear",
    ),
    "silttotal_0_cm": SoilVarSpec(
        key="silttotal_0_cm",
        field_name="silt__total",
        url="https://storage.googleapis.com/solus100pub/silttotal_0_cm_p.tif",
        resampling="bilinear",
    ),
    "dbovendry_0_cm": SoilVarSpec(
        key="dbovendry_0_cm",
        field_name="dry__bulk_density",
        url="https://storage.googleapis.com/solus100pub/dbovendry_0_cm_p.tif",
        resampling="bilinear",
    ),
}


def load_yaml(path: str | Path | None) -> dict:
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        return {}
    with open(p, "r") as f:
        return yaml.safe_load(f) or {}


def parse_soil_keys(raw: str | None) -> list[str]:
    if not raw:
        return list(DEFAULT_SOIL_SPECS.keys())
    items = [x.strip() for x in raw.split(",")]
    keys = [x for x in items if x]
    if not keys:
        raise ValueError("No keys provided after parsing --soil-keys.")
    unknown = [k for k in keys if k not in DEFAULT_SOIL_SPECS]
    if unknown:
        raise ValueError(
            f"Unknown soil keys: {unknown}. Valid keys: {list(DEFAULT_SOIL_SPECS.keys())}"
        )
    return keys


def resolve_soil_specs(keys: list[str], cfg: dict) -> list[SoilVarSpec]:
    cfg_rasters = cfg.get("feature_sources", {}).get("rasters", {})
    resolved: list[SoilVarSpec] = []
    for key in keys:
        base = DEFAULT_SOIL_SPECS[key]
        cfg_info = cfg_rasters.get(key, {})
        resolved.append(
            SoilVarSpec(
                key=base.key,
                field_name=base.field_name,
                url=cfg_info.get("url", base.url),
                resampling=cfg_info.get("resampling", base.resampling),
            )
        )
    return resolved


def resolve_aoi_path(aoi_arg: str | None, cfg: dict) -> Path:
    aoi_raw = aoi_arg or cfg.get("aoi", {}).get("aoi")
    if not aoi_raw:
        raise ValueError("AOI path is required via --aoi or config['aoi']['aoi'].")
    aoi = Path(aoi_raw)
    if not aoi.exists():
        raise FileNotFoundError(f"AOI not found: {aoi}")
    return aoi


def resolve_output_dir(output_dir_arg: str | None, cfg: dict) -> Path:
    out_dir_raw = output_dir_arg or cfg.get("paths", {}).get("output_dir")
    if not out_dir_raw:
        raise ValueError(
            "Output dir is required via --output-dir or config['paths']['output_dir']."
        )
    out_dir = Path(out_dir_raw)
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def _validate_local_source_for_aoi(aoi_path: Path, uri: str, *, label: str) -> None:
    local_path = Path(uri)
    if not local_path.exists():
        raise FileNotFoundError(f"{label} not found: {local_path}")
    if local_path.suffix.lower() == ".zip":
        return
    validate_raster_path(local_path, label=label)
    validate_aoi_overlaps_raster(aoi_path, local_path, label=label)


def _resolve_single_source(spec: RasterSourceSpec, output_dir: Path) -> Path:
    from downloads import download_file, extract_first_tif

    uri = spec.uri
    if isinstance(uri, list):
        raise TypeError("Expected a single URI, got list.")

    if uri.startswith("http"):
        local_path = output_dir / Path(uri).name
        download_file(uri, str(local_path))
        if uri.lower().endswith(".zip") or spec.unzip:
            extract_dir = output_dir / f"unzipped_{spec.key}"
            return Path(extract_first_tif(str(local_path), str(extract_dir)))
        return local_path

    local = Path(uri)
    if not local.exists():
        raise FileNotFoundError(f"Source not found: {local}")
    if local.suffix.lower() == ".zip":
        extract_dir = output_dir / f"unzipped_{spec.key}"
        return Path(extract_first_tif(str(local), str(extract_dir)))
    return local


def _resolve_source_to_tif(spec: RasterSourceSpec, output_dir: Path) -> Path:
    if isinstance(spec.uri, list):
        last_error: Exception | None = None
        for candidate in spec.uri:
            try:
                return _resolve_single_source(
                    RasterSourceSpec(
                        key=spec.key,
                        uri=candidate,
                        resampling=spec.resampling,
                        unzip=spec.unzip,
                    ),
                    output_dir,
                )
            except Exception as exc:  # pragma: no cover - best-effort fallback chain
                last_error = exc
                continue
        raise RuntimeError(f"Failed resolving source {spec.key}. Last error: {last_error}")
    return _resolve_single_source(spec, output_dir)


def _cleanup_intermediates(output_dir: Path, keep_paths: set[Path]) -> None:
    keep_resolved = {p.resolve() for p in keep_paths if p.exists()}
    for item in output_dir.iterdir():
        resolved = item.resolve()
        if resolved in keep_resolved:
            continue

        if item.is_dir() and (item.name.startswith("unzipped_") or item.name == ALIGNED_SUBDIR):
            shutil.rmtree(item, ignore_errors=True)
            continue

        if not item.is_file():
            continue

        name = item.name.lower()
        is_intermediate = (
            name.endswith(".zip")
            or "_reproj_" in name
            or "_resampled" in name
            or "_clipped" in name
            or name.startswith("usgs")
        )
        if is_intermediate:
            item.unlink(missing_ok=True)


def _write_json(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
    return path


def _manifest_common(stage: str, aoi: Path, output_dir: Path, soil_keys: list[str], options: dict) -> dict:
    return {
        "manifest_version": MANIFEST_VERSION,
        "stage": stage,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "aoi": str(aoi),
        "output_dir": str(output_dir),
        "soil_keys": soil_keys,
        "options": options,
    }


def fetch_soil_layers(
    *,
    aoi_path: Path,
    output_dir: Path,
    specs: list[SoilVarSpec],
    clip_to_aoi: bool = True,
    overwrite: bool = False,
    keep_intermediates: bool = False,
) -> Path:
    try:
        from reproject_and_resample import clip_raster_to_shape
    except ModuleNotFoundError as exc:
        if exc.name == "fiona":
            raise ImportError(
                "Missing dependency 'fiona'. Install dependencies first "
                "(recommended: conda env create -f environment.yml)."
            ) from exc
        raise

    load_and_validate_aoi(aoi_path)
    ensure_output_dir_writable(output_dir)
    for spec in specs:
        if not spec.url.startswith("http"):
            _validate_local_source_for_aoi(aoi_path, spec.url, label=f"Soil source {spec.key}")

    keep_paths: set[Path] = set()
    layers: list[dict] = []

    for spec in specs:
        final_tif = output_dir / f"{spec.key}.tif"
        if final_tif.exists() and not overwrite:
            keep_paths.add(final_tif)
            layers.append(
                {
                    "key": spec.key,
                    "field_name": spec.field_name,
                    "source_uri": spec.url,
                    "resampling": spec.resampling,
                    "fetched_tif": str(final_tif),
                    "status": "skipped_existing",
                }
            )
            continue

        src_tif = _resolve_source_to_tif(
            RasterSourceSpec(key=spec.key, uri=spec.url, resampling=spec.resampling),
            output_dir,
        )
        out_tif = src_tif
        if clip_to_aoi:
            out_tif = Path(clip_raster_to_shape(str(src_tif), str(aoi_path)))

        if final_tif.exists():
            final_tif.unlink()
        out_tif.replace(final_tif)
        keep_paths.add(final_tif)

        layers.append(
            {
                "key": spec.key,
                "field_name": spec.field_name,
                "source_uri": spec.url,
                "resampling": spec.resampling,
                "fetched_tif": str(final_tif),
                "status": "created",
            }
        )

    if not keep_intermediates:
        _cleanup_intermediates(output_dir, keep_paths)

    manifest = _manifest_common(
        stage="fetch",
        aoi=aoi_path,
        output_dir=output_dir,
        soil_keys=[s.key for s in specs],
        options={
            "clip_to_aoi": clip_to_aoi,
            "overwrite": overwrite,
            "keep_intermediates": keep_intermediates,
        },
    )
    manifest["layers"] = layers
    return _write_json(output_dir / "soil_fetch_manifest.json", manifest)


def harmonize_soil_layers(
    *,
    aoi_path: Path,
    output_dir: Path,
    specs: list[SoilVarSpec],
    grid: Grid,
    source_dir: Path | None = None,
    output_format: str = "both",
    overwrite: bool = False,
    keep_intermediates: bool = False,
) -> Path:
    """Align soil rasters onto the analysis grid and export ASC and/or GeoTIFF.

    ``grid`` comes from ``analysis_grid.grid_from_config``: the same grid the
    pipeline and every other command use.
    """
    if output_format not in {"asc", "tif", "both"}:
        raise ValueError(f"Unsupported output_format: {output_format}")

    try:
        from reproject_and_resample import convert_to_ascii
    except ModuleNotFoundError as exc:
        if exc.name == "fiona":
            raise ImportError(
                "Missing dependency 'fiona'. Install dependencies first "
                "(recommended: conda env create -f environment.yml)."
            ) from exc
        raise

    load_and_validate_aoi(aoi_path)
    ensure_output_dir_writable(output_dir)
    export_asc = output_format in {"asc", "both"}
    export_tif = output_format in {"tif", "both"}

    keep_paths: set[Path] = set()
    layers: list[dict] = []

    for spec in specs:
        out_asc = output_dir / f"{spec.field_name}.asc"
        out_tif = output_dir / f"{spec.field_name}.tif"
        wanted = [p for p, on in ((out_asc, export_asc), (out_tif, export_tif)) if on]

        if not overwrite and all(p.exists() for p in wanted):
            for existing in wanted:
                try:
                    check_on_grid(existing, grid)
                except GridMismatchError as exc:
                    raise GridMismatchError(
                        f"{exc}. It was made on a different grid; rerun with --overwrite."
                    ) from exc
            keep_paths.update(wanted)
            layers.append(_layer_record(spec, None, out_asc, out_tif, export_asc, export_tif, "skipped_existing"))
            continue

        source_tif = _soil_source_tif(spec, aoi_path, output_dir, source_dir)
        aligned = align_to_grid(
            source_tif,
            output_dir / ALIGNED_SUBDIR / f"{spec.key}.tif",
            grid,
            spec.resampling,
            aoi_path=aoi_path,
        )
        if export_asc:
            Path(convert_to_ascii(str(aligned), str(output_dir), grid=grid)).replace(out_asc)
            keep_paths.add(out_asc)
        if export_tif:
            aligned.replace(out_tif)
            keep_paths.add(out_tif)

        layers.append(_layer_record(spec, source_tif, out_asc, out_tif, export_asc, export_tif, "created"))

    if not keep_intermediates:
        _cleanup_intermediates(output_dir, keep_paths)

    manifest = _manifest_common(
        stage="harmonize",
        aoi=aoi_path,
        output_dir=output_dir,
        soil_keys=[s.key for s in specs],
        options={
            "output_format": output_format,
            "overwrite": overwrite,
            "keep_intermediates": keep_intermediates,
            "source_dir": str(source_dir) if source_dir else None,
        },
    )
    manifest.update(
        {
            "target_crs": grid.crs,
            "target_resolution": [grid.resolution, grid.resolution],
            "grid": grid.as_dict(),
            "layers": layers,
        }
    )
    return _write_json(output_dir / "soil_collection_manifest.json", manifest)


def _soil_source_tif(
    spec: SoilVarSpec, aoi_path: Path, output_dir: Path, source_dir: Path | None
) -> Path:
    if source_dir is not None:
        candidate = source_dir / f"{spec.key}.tif"
        if candidate.exists():
            validate_raster_path(candidate, label=f"Soil source {spec.key}")
            validate_aoi_overlaps_raster(aoi_path, candidate, label=f"Soil source {spec.key}")
            return candidate
    if not spec.url.startswith("http"):
        _validate_local_source_for_aoi(aoi_path, spec.url, label=f"Soil source {spec.key}")
    return _resolve_source_to_tif(
        RasterSourceSpec(key=spec.key, uri=spec.url, resampling=spec.resampling),
        output_dir,
    )


def _layer_record(
    spec: SoilVarSpec,
    source_tif: Path | None,
    out_asc: Path,
    out_tif: Path,
    export_asc: bool,
    export_tif: bool,
    status: str,
) -> dict:
    return {
        "key": spec.key,
        "field_name": spec.field_name,
        "source_uri": str(source_tif) if source_tif else spec.url,
        "resampling": spec.resampling,
        "asc": str(out_asc) if export_asc else None,
        "tif": str(out_tif) if export_tif else None,
        "status": status,
    }
