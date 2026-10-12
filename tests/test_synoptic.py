"""Offline source-to-grid tests for station forcing."""
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

import geopandas as gpd
import numpy as np
import pytest
import rasterio
from shapely.geometry import box

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from landlab_data_prep.analysis_grid import NODATA, check_on_grid, grid_for_aoi, snap_grid
from landlab_data_prep.config import ConfigError, validate_config
from landlab_data_prep.synoptic.config import SynopticConfigError, parse_synoptic_config
from landlab_data_prep.synoptic import core

BLOCK = {"start": "2025-12-07", "end": "2025-12-08", "interval_hours": 24}


@pytest.mark.parametrize("change", [{"interval_hours": True}, {"interval_hours": 5}, {"buffer_km": -1},
                                   {"max_distance_km": float("nan")}, {"end": "bad"}, {"typo": 1}])
def test_bad_config(change):
    with pytest.raises(SynopticConfigError):
        parse_synoptic_config(BLOCK | change)


def test_shared_config():
    cfg = {"aoi": {"aoi": "aoi.gpkg"}, "raster": {"target_res": 10, "resampling_method": "bilinear"},
           "paths": {"output_dir": "out"}, "synoptic": BLOCK}
    with pytest.raises(ConfigError, match="cache_dir"):
        validate_config(cfg, "synoptic")
    cfg["paths"]["cache_dir"] = "cache"
    validate_config(cfg, "synoptic")
    assert len(list(parse_synoptic_config(BLOCK).windows)) == 2


def test_nearest_and_distance_limit():
    grid = snap_grid((0, 0, 30, 10), "EPSG:32610", 10)
    values, ids, distance = core.nearest_grid([(1, 5, 5, 0), (2, 25, 5, 8)], grid,
                                             np.ones(grid.shape, bool), 0.006)
    assert values.tolist() == [[0, NODATA, 8]]
    assert ids.tolist() == [[1, NODATA, 2]]
    assert distance.tolist() == [[0, NODATA, 0]]


def test_source_to_grid_cache_and_missing(tmp_path, monkeypatch):
    aoi = tmp_path / "aoi.geojson"
    gpd.GeoDataFrame(geometry=[box(500000, 4100000, 500100, 4100100)], crs=32610).to_file(aoi)
    grid = grid_for_aoi(aoi, 50)
    centre = gpd.GeoSeries.from_xy([500025], [4100025], crs=32610).to_crs(4326).iloc[0]
    calls = []

    def get(url, params, timeout):
        calls.append(params)
        missing = params["start"].startswith("20251208")
        data = {"SUMMARY": {"RESPONSE_CODE": 1}, "UNITS": {"precipitation": "Millimeters"}, "STATION": [
            {"STID": "TEST", "LONGITUDE": centre.x, "LATITUDE": centre.y,
             "OBSERVATIONS": {"precipitation": [] if missing else [
                 {"first_report": "2025-12-07T00:00:00Z", "last_report": "2025-12-07T23:55:00Z",
                  "interval": 24, "count": 288, "total": 12.5, "report_type": "precip_accum_five_minute"}]}}]}
        class Response:
            content = json.dumps(data).encode()
            def raise_for_status(self):
                pass
        return Response()

    monkeypatch.setattr(core.requests, "get", get)
    monkeypatch.setenv("SYNOPTIC_TOKEN", "secret-test-token")
    config = parse_synoptic_config(BLOCK)
    out = tmp_path / "out"
    core.build_synoptic_forcing(config, aoi, out, grid, tmp_path / "cache")
    assert len(calls) == 2
    assert calls[0]["interval_window"] == 0
    monkeypatch.delenv("SYNOPTIC_TOKEN")
    core.build_synoptic_forcing(config, aoi, out, grid, tmp_path / "cache")
    assert len(calls) == 2
    for path in out.rglob("*.tif"):
        check_on_grid(path, grid)
    for path in out.rglob("*.asc"):
        check_on_grid(path, grid)
    with rasterio.open(out / "aligned_tif/ppt/precip_20251207T0000Z.tif") as src:
        assert np.all(src.read(1) == 12.5)
    with rasterio.open(out / "aligned_tif/ppt/precip_20251208T0000Z.tif") as src:
        assert np.all(src.read(1) == NODATA)
    rows = list(csv.DictReader((out / "forcing_synoptic.csv").open()))
    assert rows[1]["precip_mm"] == "" and rows[1]["coverage_fraction"] == "0.0"
    gauges = gpd.read_file(out / "stations.gpkg", layer="stations")
    assert gauges.crs.to_epsg() == 32610
    assert gauges.geometry.iloc[0].distance(centre_series(centre, grid.crs)) < 0.001
    assert gauges.loc[0, "valid_intervals"] == 1
    assert gauges.loc[0, "requested_intervals"] == 2
    assert gauges.loc[0, "availability_fraction"] == 0.5
    assert "secret-test-token" not in (out / "synoptic_manifest.json").read_text()


def test_interval_validation():
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    end = datetime(2025, 1, 2, tzinfo=timezone.utc)
    record = {"total": 0, "count": 24, "interval": 24,
              "first_report": start.isoformat(), "last_report": end.isoformat()}
    assert core.usable_total(record, start, end, 24)
    for change in ({"total": None}, {"total": -1}, {"total": float("nan")}, {"interval": 23}, {"count": 0}):
        assert not core.usable_total(record | change, start, end, 24)


def test_failed_response_not_cached(tmp_path, monkeypatch):
    monkeypatch.setenv("SYNOPTIC_TOKEN", "secret")
    class Response:
        content = b'{"SUMMARY":{"RESPONSE_CODE":200}}'
        def raise_for_status(self):
            pass
    monkeypatch.setattr(core.requests, "get", lambda *a, **kw: Response())
    with pytest.raises(core.SynopticError, match="200"):
        core.fetch({}, tmp_path)
    assert not list(tmp_path.rglob("*.json"))


def centre_series(point, crs):
    return gpd.GeoSeries([point], crs=4326).to_crs(crs).iloc[0]


def test_empty_station_layer(tmp_path):
    grid = snap_grid((0, 0, 10, 10), "EPSG:32610", 10)
    path = tmp_path / "stations.gpkg"
    core.write_station_layer(path, {}, {}, grid, 1)
    gauges = gpd.read_file(path, layer="stations")
    assert gauges.empty and gauges.crs.to_epsg() == 32610


def test_local_token_loading(tmp_path, monkeypatch):
    from landlab_data_prep.synoptic.cli_run import load_local_token
    config = tmp_path / "config" / "base.yaml"
    config.parent.mkdir()
    (tmp_path / ".env").write_text("SYNOPTIC_TOKEN='local-test'\n")
    monkeypatch.delenv("SYNOPTIC_TOKEN", raising=False)
    load_local_token(config)
    import os
    assert os.environ["SYNOPTIC_TOKEN"] == "local-test"
    monkeypatch.setenv("SYNOPTIC_TOKEN", "shell-test")
    load_local_token(config)
    assert os.environ["SYNOPTIC_TOKEN"] == "shell-test"
