from pathlib import Path
import importlib.util

import numpy as np
import pytest
import rasterio
from rasterio.transform import Affine, from_origin

spec = importlib.util.spec_from_file_location('download_dem', Path(__file__).parents[1] / 'scripts/download_dem.py')
dem = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dem)


def raster(path, data, transform):
    with rasterio.open(path, 'w', driver='GTiff', count=1, dtype='float32',
                       width=data.shape[1], height=data.shape[0], crs='EPSG:32610',
                       transform=transform, nodata=-9999) as dst:
        dst.write(data.astype('float32'), 1)
    return path


def test_reference_exact_grid_and_nodata(tmp_path):
    source = raster(tmp_path/'source.tif', np.full((30, 30), 120.), from_origin(499900, 4100100, 20, 20))
    data = np.ones((5, 7))
    data[2, 3] = -9999
    reference = raster(tmp_path/'reference.tif', data, Affine(13, 2, 500003, 1, -17, 4100007))
    before = source.read_bytes()
    output = tmp_path/'dem.tif'
    dem.main(['--reference', str(reference), '--source-dem', str(source), '--output', str(output), '--mask-reference'])
    with rasterio.open(output) as out, rasterio.open(reference) as ref:
        assert out.transform == ref.transform
        assert out.crs == ref.crs
        assert out.shape == ref.shape
        result = out.read(1)
        assert result[2, 3] == -9999
        assert np.allclose(result[data != -9999], 120)
    assert source.read_bytes() == before


def test_explicit_grid_and_overwrite_protection(tmp_path):
    source = raster(tmp_path/'source.tif', np.full((10, 10), 50.), from_origin(500003, 4100007, 20, 20))
    output = tmp_path/'dem.tif'
    argv = ['--aoi', str(source), '--source-dem', str(source), '--crs', 'EPSG:32610',
            '--resolution', '10', '--output', str(output)]
    dem.main(argv)
    with rasterio.open(output) as out:
        assert out.res == (10, 10)
        assert out.transform.c % 10 == 0 and out.transform.f % 10 == 0
    before = output.read_bytes()
    with pytest.raises(SystemExit):
        dem.main(argv)
    assert output.read_bytes() == before


def test_reference_rejects_conflicting_grid_options(tmp_path):
    reference = raster(tmp_path/'ref.tif', np.ones((3, 3)), from_origin(500000, 4100000, 30, 30))
    with pytest.raises(ValueError, match='omit --crs'):
        dem.target_profile(dem.read_aoi(reference), reference, 'EPSG:32610', 10)


def test_vector_aoi_download_request(tmp_path, monkeypatch):
    import geopandas as gpd
    import bmi_topography
    from shapely.geometry import box
    source = raster(tmp_path/'download.tif', np.full((30, 30), 75.), from_origin(499900, 4100100, 20, 20))
    aoi = tmp_path/'aoi.gpkg'
    gpd.GeoDataFrame(geometry=[box(500000, 4099900, 500100, 4100000)], crs='EPSG:32610').to_file(aoi)
    calls = []
    class FakeTopography:
        def __init__(self, **kwargs):
            calls.append(kwargs)
        def fetch(self):
            return source
    monkeypatch.setattr(bmi_topography, 'Topography', FakeTopography)
    monkeypatch.setenv('USGS_TOPO_API_KEY', 'test-key')
    output = tmp_path/'result.tif'
    dem.main(['--aoi', str(aoi), '--crs', 'EPSG:32610', '--resolution', '10', '--output', str(output)])
    request = calls[0]
    assert request['dem_type'] == 'USGS10m'
    assert -180 < request['west'] < request['east'] < 180
    assert -90 < request['south'] < request['north'] < 90
    with rasterio.open(output) as out:
        assert out.shape == (10, 10)
        assert np.allclose(out.read(1), 75)
