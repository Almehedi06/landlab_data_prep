"""Build NISAR backscatter and soil moisture layers on the analysis grid.

Runs on its own, separate from the Landlab pipeline, and reads the same config:
the AOI and raster settings that define the grid, paths.cache_dir, and the
nisar block.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
import sys

from landlab_data_prep.analysis_grid import grid_from_config
from landlab_data_prep.config import ConfigError, load_config
from landlab_data_prep.earthdata import EarthdataAuthError, EarthdataError
from landlab_data_prep.nisar.catalog import NisarError
from landlab_data_prep.nisar.config import parse_nisar_config
from landlab_data_prep.nisar.core import build_nisar_layers


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read NISAR scenes over the AOI and align them onto the analysis grid defined by the config."
    )
    parser.add_argument("--config", required=True, help="Config with aoi, paths, raster and nisar blocks.")
    parser.add_argument(
        "--output-dir", default=None, help="Folder for nisar_gcov/ and nisar_sme2/. Default: paths.output_dir"
    )
    parser.add_argument("--refresh", action="store_true", help="Read scenes again, replacing cached subsets.")
    parser.add_argument("--log-level", default="INFO", help="DEBUG, INFO or WARNING.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        cfg = load_config(args.config, "nisar")
    except (ConfigError, FileNotFoundError) as exc:
        raise SystemExit(str(exc)) from None

    output_dir = Path(args.output_dir or cfg["paths"]["output_dir"])
    try:
        csv_paths = build_nisar_layers(
            parse_nisar_config(cfg["nisar"]),
            cfg["aoi"]["aoi"],
            output_dir,
            grid_from_config(cfg),
            cfg["paths"]["cache_dir"],
            refresh=args.refresh,
        )
    except (NisarError, EarthdataError, EarthdataAuthError) as exc:
        raise SystemExit(str(exc)) from None

    for csv_path in csv_paths:
        print(csv_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
