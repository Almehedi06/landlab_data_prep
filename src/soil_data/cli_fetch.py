from __future__ import annotations

import argparse
from pathlib import Path

from soil_data.core import (
    fetch_soil_layers,
    load_yaml,
    parse_soil_keys,
    resolve_aoi_path,
    resolve_output_dir,
    resolve_soil_specs,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch SOLUS soil layers and crop them to the config's AOI (no grid alignment)."
    )
    parser.add_argument(
        "--config",
        default="config/base.yaml",
        help="Config that defines the AOI and analysis grid.",
    )
    parser.add_argument("--output-dir", default=None, help="Output directory for fetched TIFF files.")
    parser.add_argument("--soil-keys", default=None, help="Comma-separated subset of soil keys.")
    parser.add_argument("--no-clip", action="store_true", help="Do not crop to the AOI after download.")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing fetched TIFFs.")
    parser.add_argument(
        "--keep-intermediates",
        action="store_true",
        help="Keep temporary downloaded/intermediate files.",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    cfg = load_yaml(args.config)
    aoi = resolve_aoi_path(None, cfg)
    output_dir = resolve_output_dir(args.output_dir, cfg)
    specs = resolve_soil_specs(parse_soil_keys(args.soil_keys), cfg)

    manifest_path = fetch_soil_layers(
        aoi_path=aoi,
        output_dir=Path(output_dir),
        specs=specs,
        clip_to_aoi=not args.no_clip,
        overwrite=args.overwrite,
        keep_intermediates=args.keep_intermediates,
    )
    print(f"Saved fetch manifest: {manifest_path}")
    print(f"Soil layers fetched: {len(specs)}")


if __name__ == "__main__":
    main()
