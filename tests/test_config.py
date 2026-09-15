from __future__ import annotations

from pathlib import Path
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from landslide_data_prep.config import ConfigError, load_config, validate_config

TEMPLATE = ROOT / "config" / "base.example.yaml"


def _template() -> dict:
    return yaml.safe_load(TEMPLATE.read_text())


@pytest.mark.parametrize("command", ["pipeline", "soil", "dem_difference", "export"])
def test_template_is_valid_for_every_command(command: str) -> None:
    validate_config(_template(), command)


def test_every_problem_is_reported_at_once() -> None:
    cfg = _template()
    cfg["inputs"] = {"soil": {"k_factor": True}}
    cfg["dem"]["cache_dir"] = "/old/cache"
    cfg["raster"]["resampling_method"] = "bilinaer"
    cfg["raster"]["target_res"] = -10
    del cfg["feature_sources"]["rasters"]["claytotal_0_cm"]
    cfg["dnbr"]["enabled"] = "false"

    with pytest.raises(ConfigError) as err:
        validate_config(cfg, "pipeline", source="test.yaml")
    text = "\n".join(err.value.problems)
    for fragment in [
        "inputs: unknown key; remove it; it was never read",
        "dem.cache_dir: unknown key; use paths.cache_dir",
        "raster.resampling_method: expected one of",
        "raster.target_res: expected a positive number",
        "missing soil layers ['claytotal_0_cm']",
        "dnbr.enabled: expected true or false",
    ]:
        assert fragment in text, fragment
    assert str(err.value).startswith("Invalid config test.yaml")


def test_downloads_require_a_cache_dir_and_fire_ids() -> None:
    cfg = _template()  # DEM, soils, landcover and burn severity all download
    del cfg["paths"]["cache_dir"]
    cfg["fire"] = {"post_image_date": ""}
    cfg["dnbr"]["enabled"] = True

    with pytest.raises(ConfigError) as err:
        validate_config(cfg, "pipeline")
    text = "\n".join(err.value.problems)
    assert "paths.cache_dir: required because these sources download" in text
    assert "fire.name: required" in text and "fire.id: required" in text
    assert "fire.post_image_date: required because dNBR is downloaded" in text


def test_local_only_config_needs_no_cache_or_fire() -> None:
    cfg = _template()
    del cfg["paths"]["cache_dir"]
    del cfg["fire"]
    cfg["dem"] = {"source": "local", "path": "/data/dem.tif"}
    cfg["burn_severity"] = {"source": "local", "local": cfg["burn_severity"]["local"]}
    for info in cfg["feature_sources"]["rasters"].values():
        info["url"] = "/data/soil.tif"
    cfg["feature_sources"]["landcover"]["nlcd_2021"].update(url="/data/landcover.tif", unzip=False)
    validate_config(cfg, "pipeline")


def test_each_command_requires_only_its_sections() -> None:
    cfg = {"aoi": {"aoi": "/data/aoi.shp"}, "raster": {"target_res": 10, "resampling_method": "bilinear"}}
    validate_config(cfg, "dem_difference")
    with pytest.raises(ConfigError, match="dem: missing"):
        validate_config(cfg, "pipeline")


def test_remote_sensing_problems_are_included() -> None:
    cfg = _template()
    cfg["remote_sensing"] = {"windows": {"pre": {"start": "2018-06-01", "end": "2017-06-01"}}}
    with pytest.raises(ConfigError) as err:
        validate_config(cfg, "pipeline")
    assert any(problem.startswith("remote_sensing") for problem in err.value.problems)


def test_load_config_reads_and_validates(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "missing.yaml", "pipeline")
    path = tmp_path / "cfg.yaml"
    path.write_text(TEMPLATE.read_text())
    assert load_config(path, "soil")["raster"]["target_res"] == 10
