from __future__ import annotations

import argparse
from pathlib import Path

from landlab_data_prep.analysis_grid import grid_from_config
from landlab_data_prep.config import ConfigError, load_config
from landlab_data_prep.soil_data.core import (
    harmonize_soil_layers,
    parse_soil_keys,
    resolve_aoi_path,
    resolve_output_dir,
    resolve_soil_specs,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Align soil rasters onto the analysis grid defined by the config and export ASC/TIF outputs."
    )
    parser.add_argument(
        "--config",
        default="config/base.yaml",
        help="Config that defines the AOI and analysis grid.",
    )
    parser.add_argument("--output-dir", default=None, help="Output directory for aligned layers.")
    parser.add_argument(
        "--source-dir",
        default=None,
        help="Optional directory containing fetched source rasters named <soil_key>.tif.",
    )
    parser.add_argument("--soil-keys", default=None, help="Comma-separated subset of soil keys.")
    parser.add_argument("--format", choices=["asc", "tif", "both"], default="both", help="Output format.")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing aligned outputs.")
    parser.add_argument("--keep-intermediates", action="store_true", help="Keep aligned intermediate files.")
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
    specs = resolve_soil_specs(parse_soil_keys(args.soil_keys), cfg)
    manifest_path = harmonize_soil_layers(
        aoi_path=resolve_aoi_path(None, cfg),
        output_dir=Path(resolve_output_dir(args.output_dir, cfg)),
        specs=specs,
        grid=grid_from_config(cfg),
        source_dir=Path(args.source_dir) if args.source_dir else None,
        cache_dir=_cache_dir(cfg),
        output_format=args.format,
        overwrite=args.overwrite,
        keep_intermediates=args.keep_intermediates,
    )
    print(f"Saved harmonize manifest: {manifest_path}")
    print(f"Soil layers aligned: {len(specs)}")


if __name__ == "__main__":
    main()
