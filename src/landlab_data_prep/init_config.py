"""Write a starting config, so an installed copy needs no clone of this repo."""

from __future__ import annotations

import argparse
from importlib import resources
import os
from pathlib import Path
import sys

TEMPLATE_NAME = "base.example.yaml"


def template_text() -> str:
    """The documented config template shipped with the package."""
    return resources.files("landlab_data_prep").joinpath(TEMPLATE_NAME).read_text()


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Write a starting config to edit for your event.")
    parser.add_argument("--output", default="config/base.yaml", help="Where to write it. Default: config/base.yaml")
    parser.add_argument("--force", action="store_true", help="Overwrite the file if it exists.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    out = Path(args.output)
    if out.exists() and not args.force:
        raise SystemExit(f"{out} already exists. Pass --force to overwrite it.")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(template_text())
    if os.name == "posix":
        # The config can hold an API key, so keep it readable by its owner only.
        os.chmod(out, 0o600)
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
