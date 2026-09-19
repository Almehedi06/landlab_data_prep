"""Build daily PRISM forcing on the analysis grid.

Runs on its own, separate from the Landlab pipeline, and reads the same config:
the AOI and raster settings that define the grid, paths.cache_dir, and the
prism block.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
import sys

from landlab_data_prep.analysis_grid import grid_from_config
from landlab_data_prep.config import ConfigError, load_config
from landlab_data_prep.prism.config import parse_prism_config
from landlab_data_prep.prism.core import MANIFEST_NAME, PRISM_SUBDIR, PrismError, build_prism_forcing


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download daily PRISM grids and align them onto the analysis grid defined by the config."
    )
    parser.add_argument("--config", required=True, help="Config with aoi, paths, raster and prism blocks.")
    parser.add_argument("--output-dir", default=None, help=f"Default: <paths.output_dir>/{PRISM_SUBDIR}")
    parser.add_argument("--log-level", default="INFO", help="DEBUG, INFO or WARNING.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        cfg = load_config(args.config, "prism")
    except (ConfigError, FileNotFoundError) as exc:
        raise SystemExit(str(exc)) from None

    output_dir = Path(args.output_dir) if args.output_dir else Path(cfg["paths"]["output_dir"]) / PRISM_SUBDIR
    try:
        csv_path = build_prism_forcing(
            parse_prism_config(cfg["prism"]),
            cfg["aoi"]["aoi"],
            output_dir,
            grid_from_config(cfg),
            cfg["paths"]["cache_dir"],
        )
    except PrismError as exc:
        raise SystemExit(str(exc)) from None

    print(csv_path)
    print(output_dir / MANIFEST_NAME)
    return 0


if __name__ == "__main__":
    sys.exit(main())
