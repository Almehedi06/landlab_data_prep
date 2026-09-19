from __future__ import annotations

import argparse
import logging
from pathlib import Path

from landlab_data_prep.analysis_grid import grid_from_config
from landlab_data_prep.export_ascii_to_tif import export_ascii_dir_to_tifs
from landlab_data_prep.config import ConfigError, load_config
from landlab_data_prep.pipeline import run_landlab_pipeline, run_raster_pipeline


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run debris-flow raster/landlab pipeline.")
    parser.add_argument(
        "--config",
        required=True,
        help="Path to YAML config (e.g. config/base.yaml).",
    )
    parser.add_argument(
        "--keep-intermediates",
        action="store_true",
        help="Keep downloads and aligned intermediates (_downloads/, _aligned/).",
    )
    parser.add_argument(
        "--raster-only",
        action="store_true",
        help="Only run raster processing; skip Landlab feature generation.",
    )
    parser.add_argument(
        "--export-final-tifs",
        action="store_true",
        help="Also write a GeoTIFF next to every ASCII output.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Logging level (DEBUG, INFO, WARNING).",
    )
    return parser.parse_args()


def _resolve_output_crs(cfg: dict) -> str:
    # Exported GeoTIFFs sit on the analysis grid, so they carry its CRS.
    return grid_from_config(cfg).crs


def main() -> None:
    args = _parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO))

    try:
        cfg = load_config(args.config, "pipeline")
    except (ConfigError, FileNotFoundError) as exc:
        raise SystemExit(str(exc)) from None
    outputs = run_raster_pipeline(
        cfg,
        cleanup_intermediates=not args.keep_intermediates,
    )
    if args.raster_only:
        if args.export_final_tifs:
            crs = _resolve_output_crs(cfg)
            export_ascii_dir_to_tifs(
                Path(cfg["paths"]["output_dir"]),
                overwrite=True,
                crs=crs,
            )
        return
    run_landlab_pipeline(cfg, outputs)
    if args.export_final_tifs:
        crs = _resolve_output_crs(cfg)
        export_ascii_dir_to_tifs(
            Path(cfg["paths"]["output_dir"]),
            overwrite=True,
            crs=crs,
        )


if __name__ == "__main__":
    main()
