"""Download station rainfall and prepare analysis-grid forcing."""
import argparse
import os
from pathlib import Path

from landlab_data_prep.analysis_grid import grid_from_config
from landlab_data_prep.config import load_config
from landlab_data_prep.synoptic.config import parse_synoptic_config
from landlab_data_prep.synoptic.core import SynopticError, build_synoptic_forcing


def load_local_token(config_path):
    """Read only SYNOPTIC_TOKEN from the project .env; shell settings win."""
    if os.environ.get("SYNOPTIC_TOKEN"):
        return
    project = Path(config_path).resolve().parent
    if project.name == "config":
        project = project.parent
    path = project / ".env"
    if path.is_file():
        for line in path.read_text().splitlines():
            key, sep, value = line.strip().partition("=")
            if sep and key.strip() == "SYNOPTIC_TOKEN":
                os.environ["SYNOPTIC_TOKEN"] = value.strip().strip("\"'")
                return


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--output-dir")
    parser.add_argument("--refresh", action="store_true", help="Fetch again, replacing cached responses.")
    args = parser.parse_args(argv)
    try:
        cfg = load_config(args.config, "synoptic")
        load_local_token(args.config)
        output = Path(args.output_dir) if args.output_dir else Path(cfg["paths"]["output_dir"]) / "synoptic_forcing"
        print(build_synoptic_forcing(parse_synoptic_config(cfg["synoptic"]), cfg["aoi"]["aoi"], output,
                                    grid_from_config(cfg), cfg["paths"]["cache_dir"], args.refresh))
    except (ValueError, FileNotFoundError, SynopticError) as exc:
        raise SystemExit(str(exc)) from None
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
