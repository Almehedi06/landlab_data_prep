from __future__ import annotations

import argparse
from pathlib import Path

from analysis_grid import grid_from_config
from soil_data.core import (
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
            "Align soil rasters onto the analysis grid defined by the config "
            "(aoi.aoi and raster.target_res) and export ASC/TIF outputs."
        )
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
    parser.add_argument(
        "--format",
        choices=["asc", "tif", "both"],
        default="both",
        help="Output format for aligned layers.",
    )
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing aligned outputs.")
    parser.add_argument(
        "--keep-intermediates",
        action="store_true",
        help="Keep downloaded and aligned intermediate files.",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    cfg = load_yaml(args.config)
    grid = grid_from_config(cfg)
    aoi = resolve_aoi_path(None, cfg)
    output_dir = resolve_output_dir(args.output_dir, cfg)
    specs = resolve_soil_specs(parse_soil_keys(args.soil_keys), cfg)

    manifest_path = harmonize_soil_layers(
        aoi_path=aoi,
        output_dir=Path(output_dir),
        specs=specs,
        grid=grid,
        source_dir=Path(args.source_dir) if args.source_dir else None,
        output_format=args.format,
        overwrite=args.overwrite,
        keep_intermediates=args.keep_intermediates,
    )
    print(f"Saved harmonize manifest: {manifest_path}")
    print(f"Soil layers aligned: {len(specs)}")


if __name__ == "__main__":
    main()
