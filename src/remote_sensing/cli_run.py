"""Build HLS vegetation-index products on the analysis grid.

Runs on its own, separate from the Landlab pipeline, and reads the same config:
the AOI and raster.target_res that define the grid, plus the remote_sensing block.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
import sys

import yaml

from analysis_grid import grid_from_config, resampling_method
from remote_sensing.catalog import EarthdataAuthError
from remote_sensing.config import parse_remote_sensing_config
from remote_sensing.core import MANIFEST_NAME, REMOTE_SENSING_SUBDIR, EmptyWindowError, build_hls_products


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build HLS vegetation-index products (pre, post, change, clear-day counts) "
            "on the analysis grid defined by the config."
        )
    )
    parser.add_argument("--config", required=True, help="Config with aoi, raster and remote_sensing blocks.")
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

    config_path = Path(args.config)
    if not config_path.exists():
        raise SystemExit(f"Config not found: {config_path}")
    cfg = yaml.safe_load(config_path.read_text()) or {}
    if "remote_sensing" not in cfg:
        raise SystemExit(f"{config_path} has no remote_sensing block.")

    if args.output_dir:
        output_dir = Path(args.output_dir)
    else:
        base = (cfg.get("paths") or {}).get("output_dir")
        if not base:
            raise SystemExit("No output directory: pass --output-dir or set paths.output_dir.")
        output_dir = Path(base) / REMOTE_SENSING_SUBDIR

    # Everything checkable from the config fails here, before any search or download.
    try:
        rs_cfg = parse_remote_sensing_config(cfg["remote_sensing"])
        grid = grid_from_config(cfg)
        continuous = rs_cfg.resampling or (cfg.get("raster") or {}).get("resampling_method")
        if continuous is None:
            raise ValueError("Set raster.resampling_method or remote_sensing.resampling.")
        resampling_method(continuous)
    except ValueError as exc:  # includes RemoteSensingConfigError
        raise SystemExit(str(exc)) from exc

    try:
        products = build_hls_products(
            rs_cfg,
            cfg["aoi"]["aoi"],
            output_dir,
            grid,
            continuous_resampling=continuous,
            max_workers=args.max_workers,
        )
    except (EarthdataAuthError, EmptyWindowError) as exc:
        raise SystemExit(str(exc)) from exc

    for product in products:
        print(product.path)
    print(output_dir / MANIFEST_NAME)
    return 0


if __name__ == "__main__":
    sys.exit(main())
