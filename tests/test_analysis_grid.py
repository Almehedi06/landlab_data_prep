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

from landslide_data_prep.analysis_grid import (
    NODATA,
    GridMismatchError,
    align_to_grid,
    check_on_grid,
    grid_for_aoi,
    grid_from_config,
    resampling_method,
    snap_grid,
    utm_crs_for,
)
from landslide_data_prep.reproject_and_resample import convert_to_ascii

CRS10 = "EPSG:32610"
AOI_BOUNDS = (500010.0, 4099830.0, 500190.0, 4100010.0)  # multiples of 30 m: a 6x6 grid


def _write(path: Path, data: np.ndarray, transform, crs: str = CRS10, nodata=-9999.0) -> Path:
    with rasterio.open(
        path, "w", driver="GTiff", width=data.shape[1], height=data.shape[0], count=1,
        dtype=data.dtype, crs=crs, transform=transform, nodata=nodata,
    ) as dst:
        dst.write(data, 1)
    return path


def _aoi(path: Path, geometry, crs: str = CRS10) -> Path:
    gpd = pytest.importorskip("geopandas")
    from shapely.geometry import box

    geom = box(*geometry) if isinstance(geometry, tuple) else geometry
    gpd.GeoDataFrame({"id": [1]}, geometry=[geom], crs=crs).to_file(path, driver="ESRI Shapefile")
    return path


def test_snap_grid_snaps_outward_and_ignores_float_noise() -> None:
    grid = snap_grid((500001.0, 4099805.0, 500179.0, 4100001.0), CRS10, 30.0)
    assert (grid.transform.c, grid.transform.f, grid.width, grid.height) == (499980.0, 4100010.0, 7, 7)

    noisy = snap_grid((500010.0000000001, 4099829.9999999995, 500190.0, 4100010.0), CRS10, 30.0)
    assert (noisy.transform.c, noisy.transform.f, noisy.width, noisy.height) == (500010.0, 4100010.0, 6, 6)


def test_utm_crs_for_keeps_utm_and_picks_zone_and_hemisphere() -> None:
    gpd = pytest.importorskip("geopandas")
    from shapely.geometry import box

    nad83_utm = gpd.GeoDataFrame(geometry=[box(500000, 4100000, 500100, 4100100)], crs="EPSG:26910")
    assert utm_crs_for(nad83_utm) == "EPSG:26910"
    north = gpd.GeoDataFrame(geometry=[box(-120.4, 37.0, -120.3, 37.1)], crs="EPSG:4326")
    assert utm_crs_for(north) == "EPSG:32610"
    south = gpd.GeoDataFrame(geometry=[box(150.9, -33.9, 151.0, -33.8)], crs="EPSG:4326")
    assert utm_crs_for(south) == "EPSG:32756"


def test_grid_from_config_never_modifies_the_aoi(tmp_path: Path) -> None:
    aoi = _aoi(tmp_path / "aoi.shp", (-120.4, 37.0, -120.3, 37.1), crs="EPSG:4326")
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    grid = grid_from_config({"aoi": {"aoi": str(aoi)}, "raster": {"target_res": 30}})
    assert grid.crs == "EPSG:32610" and grid.resolution == 30.0
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before


@pytest.mark.parametrize("raster", [{}, {"target_res": 0}, {"target_res": "10"}, {"target_res": True}])
def test_grid_from_config_rejects_missing_or_bad_resolution(tmp_path: Path, raster: dict) -> None:
    aoi = _aoi(tmp_path / "aoi.shp", AOI_BOUNDS)
    with pytest.raises(ValueError, match="raster.target_res"):
        grid_from_config({"aoi": {"aoi": str(aoi)}, "raster": raster})


def test_grid_from_config_requires_an_aoi() -> None:
    with pytest.raises(ValueError, match="aoi.aoi is required"):
        grid_from_config({"raster": {"target_res": 10}})


def test_unknown_resampling_is_an_error_not_nearest() -> None:
    with pytest.raises(ValueError, match="Unknown resampling 'bilinaer'"):
        resampling_method("bilinaer")


def test_align_to_grid_masks_outside_aoi_and_never_invents_zeros(tmp_path: Path) -> None:
    from shapely.geometry import Polygon

    # uint8 source with no nodata, 20 m pixels, covering only x 500000..500100.
    src = _write(
        tmp_path / "src.tif", np.full((10, 5), 7, dtype=np.uint8),
        from_origin(500000.0, 4100020.0, 20.0, 20.0), nodata=None,
    )
    # Right triangle: cell (r, c) is inside when its centre is left of the hypotenuse.
    triangle = Polygon([(500010, 4099830), (500190, 4099830), (500010, 4100010)])
    aoi = _aoi(tmp_path / "aoi.shp", triangle)
    grid = grid_for_aoi(aoi, 30.0)

    out = align_to_grid(src, tmp_path / "out" / "aligned.tif", grid, "nearest", aoi_path=aoi)
    with rasterio.open(out) as ds:
        data = ds.read(1)
        assert ds.nodata == NODATA and ds.dtypes[0] == "float32"
    assert data[5, 0] == 7.0 and data[2, 1] == 7.0
    assert data[0, 5] == NODATA  # outside the triangle
    assert data[5, 4] == NODATA  # inside the AOI but beyond the source: nodata, not a fake 0


def test_align_to_grid_rejects_no_overlap_and_self_overwrite(tmp_path: Path) -> None:
    grid = grid_for_aoi(_aoi(tmp_path / "aoi.shp", AOI_BOUNDS), 30.0)
    far = _write(tmp_path / "far.tif", np.ones((4, 4), np.float32), from_origin(900000.0, 4100000.0, 30.0, 30.0))
    with pytest.raises(ValueError, match="no valid pixels"):
        align_to_grid(far, tmp_path / "far_aligned.tif", grid, "bilinear")
    with pytest.raises(ValueError, match="Refusing to overwrite"):
        align_to_grid(far, far, grid, "bilinear")


def test_check_on_grid_catches_shifted_geotiff_and_ascii(tmp_path: Path) -> None:
    grid = grid_for_aoi(_aoi(tmp_path / "aoi.shp", AOI_BOUNDS), 30.0)

    shifted = _write(tmp_path / "shifted.tif", np.ones(grid.shape, np.float32), from_origin(500025.0, 4100010.0, 30.0, 30.0))
    with pytest.raises(GridMismatchError, match="transform"):
        check_on_grid(shifted, grid)
    with pytest.raises(GridMismatchError):
        convert_to_ascii(str(shifted), str(tmp_path / "asc_bad"), grid=grid)

    ok = _write(tmp_path / "ok.tif", np.ones(grid.shape, np.float32), grid.transform)
    asc = Path(convert_to_ascii(str(ok), str(tmp_path / "asc"), grid=grid))
    check_on_grid(asc, grid)

    moved = tmp_path / "moved.asc"
    moved.write_text(asc.read_text().replace("xllcorner     500010.0", "xllcorner     500040.0"))
    with pytest.raises(GridMismatchError, match="xllcorner"):
        check_on_grid(moved, grid)
