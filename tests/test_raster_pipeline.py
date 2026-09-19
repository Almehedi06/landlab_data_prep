from __future__ import annotations

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

from landlab_data_prep.pipeline import (
    build_sources_from_config,
    run_landlab_pipeline,
    run_raster_pipeline,
)
from landlab_data_prep.analysis_grid import check_on_grid, grid_from_config
from landlab_data_prep.export_ascii_to_tif import export_ascii_dir_to_tifs
from landlab_data_prep.reproject_and_resample import clip_raster_to_shape, convert_to_ascii, read_ascii_header


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


def _write_aoi(path: Path, bounds, crs: str) -> None:
    geopandas = pytest.importorskip("geopandas")
    shapely_geometry = pytest.importorskip("shapely.geometry")

    polygon = shapely_geometry.box(*bounds)
    gdf = geopandas.GeoDataFrame({"id": [1]}, geometry=[polygon], crs=crs)
    gdf.to_file(path, driver="ESRI Shapefile")


def test_clip_raster_to_shape_crops_on_the_source_grid(tmp_path: Path) -> None:
    pytest.importorskip("fiona")

    crs = "EPSG:32610"
    transform = from_origin(500000.0, 4100000.0, 30.0, 30.0)
    arr = np.arange(36, dtype="float32").reshape(6, 6)
    tif_path = tmp_path / "source.tif"
    _write_tif(tif_path, arr, transform, crs)

    aoi_path = tmp_path / "aoi.shp"
    _write_aoi(aoi_path, (500030.0, 4099850.0, 500150.0, 4099970.0), crs)

    clipped = Path(clip_raster_to_shape(str(tif_path), str(aoi_path)))
    with rasterio.open(clipped) as src:
        assert (src.width, src.height) == (4, 4)
        assert src.transform.a == 30.0
        assert (src.transform.c - 500000.0) % 30.0 == 0.0  # same pixel phase as the source
        assert np.array_equal(src.read(1), arr[1:5, 1:5])

    asc = convert_to_ascii(str(clipped), str(tmp_path))
    assert read_ascii_header(asc)["ncols"] == 4


def test_raster_pipeline_reports_every_failed_source(tmp_path: Path, monkeypatch) -> None:
    from landlab_data_prep import pipeline

    aoi = tmp_path / "aoi.shp"
    _write_aoi(aoi, (500010.0, 4099830.0, 500190.0, 4100010.0), "EPSG:32610")
    cfg = {
        "aoi": {"aoi": str(aoi)},
        "paths": {"output_dir": str(tmp_path / "out")},
        "raster": {"target_res": 30.0, "resampling_method": "bilinear"},
        "dem": {"source": "local", "path": "dem.tif"},
    }
    specs = [pipeline.SourceSpec(key, f"{key}.tif", "bilinear") for key in ("a", "b", "c")]

    def fake_process(spec, *args, **kwargs):
        if spec.key == "b":
            return "b.asc"
        raise OSError(f"cannot read {spec.uri}")

    monkeypatch.setattr(pipeline, "validate_pipeline_inputs", lambda cfg: None)
    monkeypatch.setattr(pipeline, "fetch_dem", lambda *args: "dem.tif")
    monkeypatch.setattr(pipeline, "process_dem", lambda *args: "dem.asc")
    monkeypatch.setattr(pipeline, "build_sources_from_config", lambda cfg: specs)
    monkeypatch.setattr(pipeline, "process_source", fake_process)

    with pytest.raises(RuntimeError) as err:
        pipeline.run_raster_pipeline(cfg)
    message = str(err.value)
    assert "a: OSError: cannot read a.tif" in message
    assert "c: OSError: cannot read c.tif" in message
    assert "b:" not in message


def test_main_pipeline_local_smoke(tmp_path: Path) -> None:
    pytest.importorskip("fiona")
    pytest.importorskip("geopandas")
    pytest.importorskip("landlab")

    crs = "EPSG:32610"
    transform = from_origin(500000.0, 4100000.0, 30.0, 30.0)
    shape = (6, 6)

    dem = np.arange(shape[0] * shape[1], dtype="float32").reshape(shape) + 100.0
    burn = np.full(shape, 3.0, dtype="float32")
    landcover = np.full(shape, 42.0, dtype="float32")

    data_dir = tmp_path / "inputs"
    out_dir = tmp_path / "outputs"
    data_dir.mkdir()
    out_dir.mkdir()

    dem_path = data_dir / "dem.tif"
    burn_path = data_dir / "burn.tif"
    landcover_path = data_dir / "landcover.tif"
    _write_tif(dem_path, dem, transform, crs)
    _write_tif(burn_path, burn, transform, crs)
    _write_tif(landcover_path, landcover, transform, crs)

    soil_keys = [
        "cec7_0_cm",
        "anylithicdpt_cm",
        "claytotal_0_cm",
        "ph1to1h2o_0_cm",
        "sandtotal_0_cm",
        "silttotal_0_cm",
        "dbovendry_0_cm",
    ]
    soil_values = {
        "cec7_0_cm": 10.0,
        "anylithicdpt_cm": 200.0,
        "claytotal_0_cm": 25.0,
        "ph1to1h2o_0_cm": 650.0,
        "sandtotal_0_cm": 40.0,
        "silttotal_0_cm": 35.0,
        "dbovendry_0_cm": 140.0,
    }
    for key in soil_keys:
        _write_tif(data_dir / f"{key}.tif", np.full(shape, soil_values[key], dtype="float32"), transform, crs)

    with rasterio.open(dem_path) as src:
        bounds = src.bounds
    aoi_path = data_dir / "aoi.shp"
    _write_aoi(aoi_path, (bounds.left, bounds.bottom, bounds.right, bounds.top), crs)

    cfg = {
        "aoi": {"aoi": str(aoi_path)},
        "paths": {
            "output_dir": str(out_dir),
        },
        "raster": {
            "target_res": 30.0,
            "resampling_method": "bilinear",
        },
        "fire": {
            "name": "Test Fire",
            "id": "TEST000",
        },
        "dem": {
            "source": "local",
            "path": str(dem_path),
        },
        "burn_severity": {
            "source": "local",
            "local": {
                "path": str(data_dir),
                "filename": "burn.tif",
                "resampling": "nearest",
            },
        },
        "dnbr": {"enabled": False},
        "feature_sources": {
            "rasters": {
                key: {
                    "url": str(data_dir / f"{key}.tif"),
                    "resampling": "bilinear",
                }
                for key in soil_keys
            },
            "landcover": {
                "nlcd_local": {
                    "url": str(landcover_path),
                    "resampling": "nearest",
                    "unzip": False,
                }
            },
        },
    }

    outputs = run_raster_pipeline(cfg, cleanup_intermediates=False)
    grid = grid_from_config(cfg)
    for path in outputs.values():
        check_on_grid(path, grid)
    grid = run_landlab_pipeline(cfg, outputs)

    assert "soil__thickness" in grid.at_node
    assert "burn__dnbr" not in grid.at_node
    assert np.isclose(float(np.mean(grid.at_node["soil__thickness"][grid.core_nodes])), 2.0)

    expected_files = [
        out_dir / "topographic__elevation.asc",
        out_dir / "soil__thickness.asc",
        out_dir / "landcover.asc",
        out_dir / "soil__transmissivity.asc",
        out_dir / "vegetation__plant_functional_type.asc",
        out_dir / "soil__minimum_total_cohesion.asc",
        out_dir / "soil__mode_total_cohesion.asc",
        out_dir / "soil__maximum_total_cohesion.asc",
    ]
    for path in expected_files:
        assert path.exists(), f"Missing output: {path}"

    # Every written layer must export and read back: no inf or nan anywhere, and
    # cells outside the AOI stay -9999 instead of being scaled or pushed through
    # the soil formulas.
    exported, _ = export_ascii_dir_to_tifs(out_dir, overwrite=True, crs=grid_from_config(cfg).crs)
    assert exported >= len(expected_files)
    with rasterio.open(out_dir / "topographic__elevation.tif") as src:
        outside = src.read(1) == -9999.0
    assert outside.any()
    derived = {
        "soil__thickness", "soil__saturated_hydraulic_conductivity", "soil__transmissivity",
        "saturated__water_content", "porosity", "soil__density", "soil__internal_friction_angle",
    }
    for tif in sorted(out_dir.glob("*.tif")):
        with rasterio.open(tif) as src:
            data = src.read(1)
        assert np.isfinite(data).all(), f"non-finite values in {tif.name}"
        if tif.stem in derived:
            assert (data[outside] == -9999.0).all(), f"{tif.name} is not nodata outside the AOI"


def test_disabled_dnbr_needs_no_source_configuration() -> None:
    cfg = {
        "raster": {"resampling_method": "bilinear"},
        "fire": {"name": "Test Fire", "id": "TEST000"},
        "burn_severity": {
            "source": "local",
            "local": {
                "path": "/tmp",
                "filename": "burn.tif",
                "resampling": "nearest",
            },
        },
        "dnbr": {"enabled": False},
    }

    source_keys = {spec.key for spec in build_sources_from_config(cfg)}

    assert "dnbr" not in source_keys
