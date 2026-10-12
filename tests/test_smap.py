from __future__ import annotations

import csv
from datetime import date
import json
from pathlib import Path
import sys

import h5py
import numpy as np
import pytest
import rasterio
from rasterio.warp import transform_bounds
import yaml

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from landlab_data_prep import earthdata
from landlab_data_prep.analysis_grid import NODATA, check_on_grid, grid_for_aoi
from landlab_data_prep.config import ConfigError, validate_config
from landlab_data_prep.earthdata import RemoteFile
from landlab_data_prep.init_config import template_text
from landlab_data_prep.smap import core
from landlab_data_prep.smap.config import SmapConfigError, parse_smap_config
from landlab_data_prep.smap.core import (
    SmapError, build_smap_layers, granule_url, parse_granule, read_window, search_granules,
)

AOI_CRS = "EPSG:32610"
AOI_BOUNDS = (480000.0, 4080000.0, 500000.0, 4100000.0)  # 20 x 20 cells of 1 km
BLOCK = {"start": "2017-12-20", "end": "2018-01-20", "layers": ["sm_surface", "sm_rootzone"]}
TODAY = date(2026, 10, 11)
URL = "https://data.example/SMAP/{0}"
SURFACE, ROOTZONE = 0.30, 0.20


def _name(stamp: str, crid: str = "v8010", counter: str = "001") -> str:
    return f"SMAP_L4_SM_gph_{stamp}_V{crid}_{counter}.h5"


# ---------------------------------------------------------------- config


def test_parse_valid_block() -> None:
    cfg = parse_smap_config(dict(BLOCK, start=date(2017, 12, 20)), today=TODAY)
    assert (cfg.start, cfg.end, cfg.layers) == (date(2017, 12, 20), date(2018, 1, 20), ("sm_surface", "sm_rootzone"))


def test_config_reports_every_problem_at_once() -> None:
    bad = {"start": "2018-01-20", "end": "2017-12-20", "layers": ["sm_surface", "soil_temp", "sm_surface"],
           "product": "SPL4SMGP"}
    with pytest.raises(SmapConfigError) as err:
        parse_smap_config(bad, today=TODAY)
    text = "\n".join(err.value.problems)
    for fragment in ["unknown keys ['product']", "start 2018-01-20 is after end 2017-12-20",
                     "unsupported ['soil_temp']", "duplicated ['sm_surface']"]:
        assert fragment in text, fragment


def test_config_rejects_dates_smap_cannot_have() -> None:
    with pytest.raises(SmapConfigError, match="before SMAP L4 begins"):
        parse_smap_config(dict(BLOCK, start="2015-03-01", end="2015-04-01"), today=TODAY)
    with pytest.raises(SmapConfigError, match="in the future"):
        parse_smap_config(dict(BLOCK, start="2026-10-01", end="2026-10-12"), today=TODAY)


def test_example_smap_block_in_template_is_valid() -> None:
    text = template_text()
    lines = []
    for line in text[text.index("# smap:"):].splitlines():
        if not line.startswith("#"):
            break
        lines.append(line[2:] if line.startswith("# ") else line[1:])
    parse_smap_config(yaml.safe_load("\n".join(lines))["smap"], today=TODAY)


def test_smap_command_needs_the_block_and_a_cache() -> None:
    cfg = {"aoi": {"aoi": "/a.shp"}, "paths": {"output_dir": "/out"},
           "raster": {"target_res": 10, "resampling_method": "bilinear"}}
    with pytest.raises(ConfigError) as err:
        validate_config(cfg, "smap")
    text = "\n".join(err.value.problems)
    assert "smap: missing" in text and "paths.cache_dir: required because SMAP windows are cached" in text


# ---------------------------------------------------------------- catalog


def test_file_names_and_links() -> None:
    granule = parse_granule(_name("20180109T103000"), "u")
    assert granule.time.isoformat() == "2018-01-09T10:30:00+00:00" and granule.stem == "20180109T1030"
    with pytest.raises(SmapError, match="Unrecognised"):
        parse_granule("SMAP_L3_SM_P_E_20180109_R16020_001.h5", "u")
    name = _name("20180109T103000")
    entry = {"title": name, "links": [
        {"rel": earthdata.DATA_REL, "href": URL.format(name)},
        {"rel": earthdata.DATA_REL, "href": "https://search.earthdata.nasa.gov/search/granules?p=C1"},
        {"rel": earthdata.DATA_REL, "href": "https://appeears.earthdatacloud.nasa.gov/"},
    ]}
    assert granule_url(entry) == URL.format(name)
    # A reprocessed granule: CMR suffixes its title; the file keeps its name.
    newer = _name("20180109T103000", crid="v8013")
    reprocessed = {"title": f"{newer}_u9loH5HD", "producer_granule_id": newer, "links": [
        {"rel": earthdata.DATA_REL, "href": URL.format(newer)},
        {"rel": earthdata.DATA_REL, "href": URL.format(newer.replace(".h5", ".qa"))},
    ]}
    assert granule_url(reprocessed) == URL.format(newer)


def test_search_keeps_one_file_per_step_inside_the_days(monkeypatch) -> None:
    names = [_name("20171219T223000"), _name("20171220T013000"), _name("20171220T013000", counter="002"),
             _name("20171220T223000"), _name("20171220T223000", crid="v8013"), _name("20171221T013000")]

    class Page:
        headers: dict = {}

        def raise_for_status(self):
            pass

        def json(self):
            return {"feed": {"entry": [
                {"title": f"{n}_x1" if "v8013" in n else n, "producer_granule_id": n,
                 "links": [{"rel": earthdata.DATA_REL, "href": URL.format(n)}]} for n in names
            ]}}

    monkeypatch.setattr(earthdata.requests, "get", lambda *a, **k: Page())
    granules, search = search_granules((-120, 34, -119, 35), date(2017, 12, 20), date(2017, 12, 20))
    assert [g.id for g in granules] == [_name("20171220T013000", counter="002"),
                                        _name("20171220T223000", crid="v8013")]
    assert search["granules_found"] == 6


# ---------------------------------------------------------------- synthetic SMAP L4 file


def _smap_file(path: Path, surface: float = SURFACE) -> Path:
    """A file laid out like SMAP L4: 9 km EASE-Grid 2.0 centres, one chunk per layer, -9999 fill."""
    res = 9008.055210146
    west, south, east, north = transform_bounds(AOI_CRS, "EPSG:6933", *AOI_BOUNDS)
    x = np.arange(west - 30000, east + 30000, res) + res / 2
    y = np.arange(north + 30000, south - 30000, -res) - res / 2
    shape = (len(y), len(x))
    sm_surface = np.full(shape, surface, np.float32)
    sm_surface[:, x < west + res] = -9999.0  # ocean along the western edge
    sm_rootzone = np.full(shape, ROOTZONE, np.float32)
    with h5py.File(path, "w") as f:
        f["x"], f["y"] = x, y
        projection = f.create_dataset("EASE2_global_projection", data=np.array([b""], dtype="S1"))
        projection.attrs["grid_mapping_name"] = np.bytes_(b"lambert_cylindrical_equal_area")
        projection.attrs["standard_parallel"] = np.float64(30.0)
        for name, data in (("sm_surface", sm_surface), ("sm_rootzone", sm_rootzone)):
            d = f.create_dataset(f"Geophysical_Data/{name}", data=data, chunks=shape, compression="gzip")
            d.attrs["_FillValue"] = np.float32(-9999.0)
    return path


def test_window_keeps_values_and_blanks_fill(tmp_path) -> None:
    with h5py.File(_smap_file(tmp_path / "f.h5"), "r") as h5:
        transform, data = read_window(h5, ("sm_surface", "sm_rootzone"), AOI_BOUNDS, AOI_CRS)
    surface = data["sm_surface"]
    assert np.any(surface == NODATA) and np.all(surface[surface != NODATA] == np.float32(SURFACE))
    assert np.all(data["sm_rootzone"] == np.float32(ROOTZONE))
    assert transform.a == pytest.approx(9008.055210146) and transform.e == pytest.approx(-9008.055210146)


# ---------------------------------------------------------------- end to end


class FakeResponse:
    def __init__(self, status, content=b"", headers=None, url=""):
        self.status_code, self.content, self.headers, self.url = status, content, headers or {}, url

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeNSIDC:
    def __init__(self, files: dict[str, bytes]):
        self.files, self.reads = files, 0

    def get(self, url, headers=None, timeout=None, stream=False):
        body = self.files[url]
        start, end = (int(v) for v in headers["Range"].split("=")[1].split("-"))
        self.reads += 1
        return FakeResponse(206, body[start:end + 1], {"Content-Range": f"bytes {start}-{end}/{len(body)}"}, url)


@pytest.fixture
def smap_env(tmp_path, monkeypatch):
    gpd = pytest.importorskip("geopandas")
    from shapely.geometry import box

    aoi = tmp_path / "aoi.shp"
    gpd.GeoDataFrame({"id": [1]}, geometry=[box(*AOI_BOUNDS)], crs=AOI_CRS).to_file(aoi)
    granules = [parse_granule(n, URL.format(n)) for n in (_name("20180109T073000"), _name("20180109T103000"))]
    server = FakeNSIDC({
        granules[0].url: _smap_file(tmp_path / "a.h5").read_bytes(),
        granules[1].url: _smap_file(tmp_path / "b.h5", surface=0.40).read_bytes(),
    })
    monkeypatch.setattr(core, "search_granules", lambda *a: (granules, {"granules_found": 2}))
    return aoi, grid_for_aoi(aoi, 1000.0), tmp_path, server


def test_soil_moisture_lands_on_the_grid_with_daily_means(smap_env) -> None:
    aoi, grid, tmp, server = smap_env
    index = build_smap_layers(parse_smap_config(BLOCK, today=TODAY), aoi, tmp / "out", grid, tmp / "cache",
                              open_file=lambda g: RemoteFile(g.url, server))
    rows = list(csv.DictReader(open(index)))
    assert [r["datetime"] for r in rows] == ["2018-01-09T07:30:00+00:00", "2018-01-09T10:30:00+00:00"]
    assert float(rows[0]["sm_surface_mean"]) == pytest.approx(SURFACE)
    assert float(rows[1]["sm_surface_mean"]) == pytest.approx(0.40)
    assert float(rows[0]["sm_rootzone_coverage"]) == 1.0
    assert rows[0]["sm_surface_tif_path"] == "aligned_tif/sm_surface/sm_surface_20180109T0730.tif"
    for row in rows:
        for layer in ("sm_surface", "sm_rootzone"):
            check_on_grid(tmp / "out" / row[f"{layer}_tif_path"], grid)
    with rasterio.open(tmp / "out" / rows[0]["sm_surface_tif_path"]) as src:
        values = np.unique(src.read(1))
    assert set(values.tolist()) <= {NODATA, np.float32(SURFACE)}  # nearest copies cells, never blends them

    [day] = list(csv.DictReader(open(tmp / "out" / "smap_l4_daily.csv")))
    assert day["date"] == "2018-01-09" and day["steps"] == "2"
    assert float(day["sm_surface_mean"]) == pytest.approx((SURFACE + 0.40) / 2)
    manifest = json.loads((tmp / "out" / "smap_manifest.json").read_text())
    assert manifest["files"] == 2 and manifest["downloaded"] == 2 and manifest["resampling"] == "nearest"


def test_cached_windows_are_reused_without_reading(smap_env) -> None:
    aoi, grid, tmp, server = smap_env
    cfg = parse_smap_config(BLOCK, today=TODAY)
    build_smap_layers(cfg, aoi, tmp / "out", grid, tmp / "cache", open_file=lambda g: RemoteFile(g.url, server))

    def no_reads(granule):
        raise AssertionError("cached file was read again")

    build_smap_layers(cfg, aoi, tmp / "again", grid, tmp / "cache", open_file=no_reads)
    manifest = json.loads((tmp / "again" / "smap_manifest.json").read_text())
    assert manifest["reused_from_cache"] == 2


def test_no_files_is_an_error(smap_env, monkeypatch) -> None:
    aoi, grid, tmp, server = smap_env
    monkeypatch.setattr(core, "search_granules", lambda *a: ([], {}))
    with pytest.raises(SmapError, match="No SMAP L4 files"):
        build_smap_layers(parse_smap_config(BLOCK, today=TODAY), aoi, tmp / "out", grid, tmp / "cache",
                          open_file=lambda g: RemoteFile(g.url, server))
