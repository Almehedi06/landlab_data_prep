from __future__ import annotations

import json
from pathlib import Path
import sys

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from landlab_data_prep.analysis_grid import GridMismatchError, check_on_grid, grid_for_aoi
from landlab_data_prep.soil_data.core import (
    DEFAULT_SOIL_SPECS,
    SoilVarSpec,
    harmonize_soil_layers,
    parse_soil_keys,
    resolve_soil_specs,
)


def _write_tif(path: Path, data: np.ndarray, transform, crs: str, nodata: float = -9999.0) -> None:
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=data.shape[1],
        height=data.shape[0],
        count=1,
        dtype="float32",
        crs=crs,
        transform=transform,
        nodata=nodata,
    ) as dst:
        dst.write(data.astype("float32"), 1)


def test_parse_soil_keys_default() -> None:
    assert parse_soil_keys(None) == list(DEFAULT_SOIL_SPECS.keys())


def test_parse_soil_keys_subset() -> None:
    keys = parse_soil_keys("cec7_0_cm,claytotal_0_cm")
    assert keys == ["cec7_0_cm", "claytotal_0_cm"]


def test_parse_soil_keys_unknown() -> None:
    with pytest.raises(ValueError):
        parse_soil_keys("does_not_exist")


def test_resolve_soil_specs_override() -> None:
    cfg = {
        "feature_sources": {
            "rasters": {
                "cec7_0_cm": {
                    "url": "file:///tmp/custom.tif",
                    "resampling": "nearest",
                }
            }
        }
    }
    specs = resolve_soil_specs(["cec7_0_cm"], cfg)
    assert len(specs) == 1
    assert specs[0].url == "file:///tmp/custom.tif"
    assert specs[0].resampling == "nearest"


def _soil_case(tmp_path: Path):
    pytest.importorskip("fiona")
    geopandas = pytest.importorskip("geopandas")
    shapely_geometry = pytest.importorskip("shapely.geometry")

    crs = "EPSG:32610"
    source_dir = tmp_path / "raw"
    source_dir.mkdir(parents=True, exist_ok=True)
    # 20 m source pixels on an origin that does not line up with the 30 m grid.
    _write_tif(
        source_dir / "cec7_0_cm.tif",
        np.ones((11, 11), dtype="float32"),
        from_origin(499990.0, 4100030.0, 20.0, 20.0),
        crs,
    )
    aoi_path = tmp_path / "aoi.shp"
    polygon = shapely_geometry.box(500010.0, 4099830.0, 500190.0, 4100010.0)
    geopandas.GeoDataFrame({"id": [1]}, geometry=[polygon], crs=crs).to_file(aoi_path, driver="ESRI Shapefile")
    spec = SoilVarSpec(
        key="cec7_0_cm",
        field_name="cation__exchange_capacity",
        url="unused_for_this_test",
        resampling="bilinear",
    )
    return aoi_path, source_dir, spec


def test_harmonize_puts_layers_on_the_analysis_grid(tmp_path: Path) -> None:
    aoi_path, source_dir, spec = _soil_case(tmp_path)
    grid = grid_for_aoi(aoi_path, 30.0)
    out_dir = tmp_path / "harmonized"

    manifest_path = harmonize_soil_layers(
        aoi_path=aoi_path,
        output_dir=out_dir,
        specs=[spec],
        source_dir=source_dir,
        grid=grid,
        output_format="both",
        overwrite=True,
        keep_intermediates=False,
    )

    for name in ("cation__exchange_capacity.tif", "cation__exchange_capacity.asc"):
        check_on_grid(out_dir / name, grid)
    with rasterio.open(out_dir / "cation__exchange_capacity.tif") as out:
        assert np.allclose(out.read(1), 1.0)
    assert not (out_dir / "_aligned").exists()

    with open(manifest_path, "r") as f:
        manifest = json.load(f)
    assert manifest["manifest_version"] == "1.0"
    assert manifest["stage"] == "harmonize"
    assert manifest["grid"] == grid.as_dict()


def test_harmonize_refuses_outputs_left_from_another_grid(tmp_path: Path) -> None:
    aoi_path, source_dir, spec = _soil_case(tmp_path)
    out_dir = tmp_path / "harmonized"
    common = dict(aoi_path=aoi_path, output_dir=out_dir, specs=[spec], source_dir=source_dir)

    harmonize_soil_layers(grid=grid_for_aoi(aoi_path, 30.0), overwrite=True, **common)
    with pytest.raises(GridMismatchError, match="--overwrite"):
        harmonize_soil_layers(grid=grid_for_aoi(aoi_path, 10.0), **common)
