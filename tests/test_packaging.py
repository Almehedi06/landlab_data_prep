"""The install itself: one dependency list, a template that ships, and a command that writes it."""

from __future__ import annotations

import os
from pathlib import Path
import sys

import pytest
import yaml

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10; the CI matrix covers these checks on 3.11+
    tomllib = None

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from landlab_data_prep import __version__
from landlab_data_prep.config import validate_config
from landlab_data_prep.init_config import main, template_text

PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text()) if tomllib else {}
needs_toml = pytest.mark.skipif(tomllib is None, reason="pyproject.toml needs tomllib, which is Python 3.11+")


def _name(requirement: str) -> str:
    """The package name in 'rasterio>=1.3' or a conda 'python=3.10'."""
    for separator in (">=", "==", "<=", "~=", ">", "<", "="):
        requirement = requirement.split(separator)[0]
    return requirement.strip()


@needs_toml
def test_conda_and_pip_declare_the_same_dependencies() -> None:
    pip = {_name(r) for r in PYPROJECT["project"]["dependencies"]}
    dev = {_name(r) for r in PYPROJECT["project"]["optional-dependencies"]["dev"]}
    conda = yaml.safe_load((ROOT / "environment.yml").read_text())["dependencies"]
    conda = {_name(d) for d in conda if isinstance(d, str)} - {"python", "pip"}
    assert conda == pip | dev, f"only in environment.yml: {conda - pip - dev}; only in pyproject: {(pip | dev) - conda}"


@needs_toml
def test_every_command_points_at_a_real_function() -> None:
    from importlib import import_module

    for command, target in PYPROJECT["project"]["scripts"].items():
        module, _, function = target.partition(":")
        assert callable(getattr(import_module(module), function)), f"{command} -> {target}"


def test_the_version_is_declared_once() -> None:
    assert __version__ in (ROOT / "CHANGELOG.md").read_text()
    if tomllib:
        assert PYPROJECT["project"]["dynamic"] == ["version"]  # setuptools reads __version__


def test_the_template_ships_with_the_package_and_is_valid() -> None:
    cfg = yaml.safe_load(template_text())
    validate_config(cfg, "pipeline")  # the template is a working config, not just documentation


def test_init_config_writes_a_private_file_and_refuses_to_clobber(tmp_path: Path, capsys) -> None:
    out = tmp_path / "config" / "base.yaml"
    assert main(["--output", str(out)]) == 0
    assert out.read_text() == template_text()
    if os.name == "posix":  # Windows has no Unix file modes
        assert oct(out.stat().st_mode)[-3:] == "600"  # it can hold an API key
    assert str(out) in capsys.readouterr().out

    out.write_text("edited by hand\n")
    with pytest.raises(SystemExit, match="already exists"):
        main(["--output", str(out)])
    assert out.read_text() == "edited by hand\n"

    assert main(["--output", str(out), "--force"]) == 0
    assert out.read_text() == template_text()
