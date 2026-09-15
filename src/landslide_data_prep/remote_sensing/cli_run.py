"""Build HLS vegetation-index products on the analysis grid.

Runs on its own, separate from the Landlab pipeline, and reads the same config:
the AOI and raster settings that define the grid, plus the remote_sensing block.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
import sys

from landslide_data_prep.analysis_grid import grid_from_config
from landslide_data_prep.config import ConfigError, load_config
from landslide_data_prep.remote_sensing.catalog import EarthdataAuthError
from landslide_data_prep.remote_sensing.config import parse_remote_sensing_config
from landslide_data_prep.remote_sensing.core import (
    MANIFEST_NAME,
    REMOTE_SENSING_SUBDIR,
    EmptyWindowError,
    build_hls_products,
)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build HLS vegetation-index products (pre, post, change, clear-day counts) "
            "on the analysis grid defined by the config."
        )
    )
    parser.add_argument("--config", required=True, help="Config with aoi, paths, raster and remote_sensing blocks.")
    parser.add_argument(
        "--output-dir",
        default=None,
        help=f"Default: <paths.output_dir>/{REMOTE_SENSING_SUBDIR}",
    )
    parser.add_argument("--max-workers", type=int, default=8, help="Parallel scene reads. Default: 8.")
    parser.add_argument("--log-level", default="INFO", help="DEBUG, INFO or WARNING.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(levelname)s %(name)s: %(message)s",
    )
    if args.max_workers < 1:
        raise SystemExit("--max-workers must be at least 1.")

    try:
        cfg = load_config(args.config, "remote_sensing")
    except (ConfigError, FileNotFoundError) as exc:
        raise SystemExit(str(exc)) from None

    rs_cfg = parse_remote_sensing_config(cfg["remote_sensing"])
    output_dir = Path(args.output_dir) if args.output_dir else Path(cfg["paths"]["output_dir"]) / REMOTE_SENSING_SUBDIR
    try:
        products = build_hls_products(
            rs_cfg,
            cfg["aoi"]["aoi"],
            output_dir,
            grid_from_config(cfg),
            continuous_resampling=rs_cfg.resampling or cfg["raster"]["resampling_method"],
            max_workers=args.max_workers,
        )
    except (EarthdataAuthError, EmptyWindowError) as exc:
        raise SystemExit(str(exc)) from None

    for product in products:
        print(product.path)
    print(output_dir / MANIFEST_NAME)
    return 0


if __name__ == "__main__":
    sys.exit(main())
