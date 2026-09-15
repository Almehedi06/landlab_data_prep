from __future__ import annotations

import argparse
from pathlib import Path

from analysis_grid import grid_from_config
from soil_data.core import (
    fetch_soil_layers,
    harmonize_soil_layers,
    load_yaml,
    parse_soil_keys,
    resolve_aoi_path,
    resolve_output_dir,
    resolve_soil_specs,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run the soil workflow end to end: fetch and crop to the AOI, then align "
            "onto the analysis grid defined by the config."
        )
    )
    parser.add_argument(
        "--config",
        default="config/base.yaml",
        help="Config that defines the AOI and analysis grid.",
    )
    parser.add_argument("--output-dir", default=None, help="Output directory for aligned layers.")
    parser.add_argument(
        "--raw-dir",
        default=None,
        help="Directory for fetched rasters. Default: <output-dir>/soil_raw",
    )
    parser.add_argument("--soil-keys", default=None, help="Comma-separated subset of soil keys.")
    parser.add_argument(
        "--format",
        choices=["asc", "tif", "both"],
        default="both",
        help="Output format for aligned layers.",
    )
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing outputs.")
    parser.add_argument(
        "--keep-intermediates",
        action="store_true",
        help="Keep temporary files in both fetch and align stages.",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    cfg = load_yaml(args.config)
    grid = grid_from_config(cfg)
    aoi = resolve_aoi_path(None, cfg)
    output_dir = resolve_output_dir(args.output_dir, cfg)
    raw_dir = Path(args.raw_dir) if args.raw_dir else (Path(output_dir) / "soil_raw")
    raw_dir.mkdir(parents=True, exist_ok=True)
    specs = resolve_soil_specs(parse_soil_keys(args.soil_keys), cfg)

    fetch_manifest = fetch_soil_layers(
        aoi_path=aoi,
        output_dir=raw_dir,
        specs=specs,
        clip_to_aoi=True,
        overwrite=args.overwrite,
        keep_intermediates=args.keep_intermediates,
    )
    print(f"Saved fetch manifest: {fetch_manifest}")

    harmonize_manifest = harmonize_soil_layers(
        aoi_path=aoi,
        output_dir=Path(output_dir),
        specs=specs,
        grid=grid,
        source_dir=raw_dir,
        output_format=args.format,
        overwrite=args.overwrite,
        keep_intermediates=args.keep_intermediates,
    )
    print(f"Saved harmonize manifest: {harmonize_manifest}")
    print(f"Soil layers processed: {len(specs)}")


if __name__ == "__main__":
    main()
