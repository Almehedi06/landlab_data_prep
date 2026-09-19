from __future__ import annotations

import argparse
import glob
from pathlib import Path

import numpy as np
import rasterio

from landlab_data_prep.analysis_grid import grid_from_config
from landlab_data_prep.config import ConfigError, load_config


def _resolve_output_dir(cfg: dict, output_dir: str | None) -> Path:
    if output_dir:
        return Path(output_dir)
    out = (cfg.get("paths") or {}).get("output_dir")
    if not out:
        raise ValueError("No output directory: pass --output-dir or set paths.output_dir.")
    return Path(out)


def _asc_to_tif(asc_path: Path, tif_path: Path, overwrite: bool, crs: str | None) -> bool:
    if tif_path.exists() and not overwrite:
        return False

    with rasterio.open(asc_path) as src:
        data = src.read(1)
        meta = src.meta.copy()

    meta.update(driver="GTiff", count=1)
    if meta.get("nodata") is None:
        if np.any(data == -9999):
            meta["nodata"] = -9999.0
        else:
            finite = data[np.isfinite(data)]
            if finite.size and np.max(np.abs(finite)) > 1e20:
                # Common float sentinel seen in some ASC exports.
                meta["nodata"] = float(np.max(finite))
    if crs:
        meta.update(crs=crs)
    with rasterio.open(tif_path, "w", **meta) as dst:
        dst.write(data, 1)
    return True


def export_ascii_dir_to_tifs(
    out_dir: Path,
    pattern: str = "*.asc",
    overwrite: bool = True,
    crs: str | None = None,
) -> tuple[int, int]:
    asc_files = [Path(p) for p in glob.glob(str(out_dir / pattern))]
    if not asc_files:
        return 0, 0

    exported = 0
    skipped = 0
    for asc_path in asc_files:
        tif_path = asc_path.with_suffix(".tif")
        if _asc_to_tif(asc_path, tif_path, overwrite, crs):
            exported += 1
        else:
            skipped += 1
    return exported, skipped


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export all ESRI ASCII grids in output_dir to GeoTIFF."
    )
    parser.add_argument(
        "--config",
        required=True,
        help="Config YAML. Sets the CRS from its analysis grid, and the output dir unless --output-dir is given.",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Directory containing .asc files (overrides config).",
    )
    parser.add_argument(
        "--pattern",
        default="*.asc",
        help="Glob pattern for ASCII files (default: *.asc).",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing .tif files.",
    )
    args = parser.parse_args()

    try:
        cfg = load_config(args.config, "export")
    except (ConfigError, FileNotFoundError) as exc:
        raise SystemExit(str(exc)) from None
    out_dir = _resolve_output_dir(cfg, args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    # ESRI ASCII grids carry no CRS; the analysis grid the config defines does.
    crs = grid_from_config(cfg).crs

    exported, skipped = export_ascii_dir_to_tifs(
        out_dir,
        pattern=args.pattern,
        overwrite=args.overwrite,
        crs=crs,
    )
    if exported == 0 and skipped == 0:
        print(f"No ASCII files found in {out_dir} matching {args.pattern}")
        return
    print(f"Exported {exported} GeoTIFF(s). Skipped {skipped}.")


if __name__ == "__main__":
    main()
