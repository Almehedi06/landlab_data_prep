from __future__ import annotations

import argparse
from pathlib import Path

from landslide_data_prep.analysis_grid import grid_from_config
from landslide_data_prep.config import ConfigError, load_config
from landslide_data_prep.soil_data.core import (
    fetch_soil_layers,
    harmonize_soil_layers,
    parse_soil_keys,
    resolve_aoi_path,
    resolve_output_dir,
    resolve_soil_specs,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch soil layers, crop them to the AOI, then align them onto the analysis grid."
    )
    parser.add_argument(
        "--config",
        default="config/base.yaml",
        help="Config that defines the AOI and analysis grid.",
    )
    parser.add_argument("--output-dir", default=None, help="Output directory for aligned layers.")
    parser.add_argument("--raw-dir", default=None, help="Directory for fetched rasters. Default: <output-dir>/soil_raw")
    parser.add_argument("--soil-keys", default=None, help="Comma-separated subset of soil keys.")
    parser.add_argument("--format", choices=["asc", "tif", "both"], default="both", help="Output format.")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing outputs.")
    parser.add_argument("--keep-intermediates", action="store_true", help="Keep temporary files in both stages.")
    return parser.parse_args()


def _load(config: str) -> dict:
    try:
        return load_config(config, "soil")
    except (ConfigError, FileNotFoundError) as exc:
        raise SystemExit(str(exc)) from None


def _cache_dir(cfg: dict) -> Path | None:
    value = cfg["paths"].get("cache_dir")
    return Path(value) if value else None


def main() -> None:
    args = _parse_args()
    cfg = _load(args.config)
    aoi = resolve_aoi_path(None, cfg)
    output_dir = Path(resolve_output_dir(args.output_dir, cfg))
    raw_dir = Path(args.raw_dir) if args.raw_dir else output_dir / "soil_raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    specs = resolve_soil_specs(parse_soil_keys(args.soil_keys), cfg)

    fetch_manifest = fetch_soil_layers(
        aoi_path=aoi,
        output_dir=raw_dir,
        specs=specs,
        cache_dir=_cache_dir(cfg),
        clip_to_aoi=True,
        overwrite=args.overwrite,
        keep_intermediates=args.keep_intermediates,
    )
    print(f"Saved fetch manifest: {fetch_manifest}")

    harmonize_manifest = harmonize_soil_layers(
        aoi_path=aoi,
        output_dir=output_dir,
        specs=specs,
        grid=grid_from_config(cfg),
        source_dir=raw_dir,
        output_format=args.format,
        overwrite=args.overwrite,
        keep_intermediates=args.keep_intermediates,
    )
    print(f"Saved harmonize manifest: {harmonize_manifest}")
    print(f"Soil layers processed: {len(specs)}")


if __name__ == "__main__":
    main()
