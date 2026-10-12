"""Build SMAP L4 soil moisture layers on the analysis grid.

Runs on its own, separate from the Landlab pipeline, and reads the same config:
the AOI and raster settings that define the grid, paths.cache_dir, and the
smap block.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
import sys

from landlab_data_prep.analysis_grid import grid_from_config
from landlab_data_prep.config import ConfigError, load_config
from landlab_data_prep.earthdata import EarthdataAuthError, EarthdataError
from landlab_data_prep.smap.config import parse_smap_config
from landlab_data_prep.smap.core import SMAP_SUBDIR, SmapError, build_smap_layers


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read SMAP L4 soil moisture over the AOI and align it onto the analysis grid defined by the config."
    )
    parser.add_argument("--config", required=True, help="Config with aoi, paths, raster and smap blocks.")
    parser.add_argument("--output-dir", default=None, help=f"Default: <paths.output_dir>/{SMAP_SUBDIR}")
    parser.add_argument("--refresh", action="store_true", help="Read files again, replacing cached windows.")
    parser.add_argument("--log-level", default="INFO", help="DEBUG, INFO or WARNING.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        cfg = load_config(args.config, "smap")
    except (ConfigError, FileNotFoundError) as exc:
        raise SystemExit(str(exc)) from None

    output_dir = Path(args.output_dir) if args.output_dir else Path(cfg["paths"]["output_dir"]) / SMAP_SUBDIR
    try:
        index_path = build_smap_layers(
            parse_smap_config(cfg["smap"]),
            cfg["aoi"]["aoi"],
            output_dir,
            grid_from_config(cfg),
            cfg["paths"]["cache_dir"],
            refresh=args.refresh,
        )
    except (SmapError, EarthdataError, EarthdataAuthError) as exc:
        raise SystemExit(str(exc)) from None

    print(index_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
