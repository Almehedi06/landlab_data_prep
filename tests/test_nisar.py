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
import requests
import yaml

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from landlab_data_prep.analysis_grid import NODATA, check_on_grid, grid_for_aoi
from landlab_data_prep.config import ConfigError, validate_config
from landlab_data_prep.init_config import template_text
from landlab_data_prep import earthdata
from landlab_data_prep.earthdata import EARTHDATA_HOST, EarthdataAuthError, RemoteFile, earthdata_session
from landlab_data_prep.nisar import catalog, core
from landlab_data_prep.nisar.catalog import NisarError, data_url, latest_processing, parse_scene
from landlab_data_prep.nisar.config import NisarConfigError, parse_nisar_config
from landlab_data_prep.nisar.core import build_nisar_layers, read_native_window

AOI_CRS = "EPSG:32610"
AOI_BOUNDS = (500010.0, 4099830.0, 500190.0, 4100010.0)  # 6 x 6 cells of 30 m
SM_AOI_BOUNDS = (500000.0, 4098000.0, 502000.0, 4100000.0)  # 20 x 20 cells of 100 m
GCOV_BLOCK = {"start": "2025-12-07", "end": "2025-12-20", "layers": {"GCOV": ["HHHH", "HVHV"]}}
SM_BLOCK = {"start": "2025-12-07", "end": "2025-12-20", "layers": {"SME2": ["combined", "DSG", "TSR", "recommended"]}}
TODAY = date(2026, 10, 11)
NAME = "NISAR_L2_PR_GCOV_032_071_D_064_4005_DHDH_A_20261005T030953_20261005T031029_P05023_N_F_J_001"
FAR_NAME = "NISAR_L2_PR_GCOV_032_077_A_026_4005_DHDH_A_20261005T124722_20261005T124757_P05023_N_F_J_001"
SM_NAME = "NISAR_L3_PR_SME2_007_172_D_064_4005_DHDH_A_20251216T031815_20251216T031850_X05010_N_F_J_001"
URL = "https://nisar.asf.example/NISAR/{0}/{0}.h5"
HH, HV = 0.2, 0.05
COMBINED, TSR = 0.25, 0.12


# ---------------------------------------------------------------- config


def test_parse_valid_block() -> None:
    block = {"start": date(2025, 12, 7), "end": "2025-12-20",
             "layers": {"SME2": ["TSR", "combined"], "GCOV": ["HHHH"]}}
    cfg = parse_nisar_config(block, today=TODAY)
    assert (cfg.start, cfg.end) == (date(2025, 12, 7), date(2025, 12, 20))
    assert list(cfg.layers.items()) == [("GCOV", ("HHHH",)), ("SME2", ("TSR", "combined"))]


def test_config_reports_every_problem_at_once() -> None:
    bad = {"product": "GCOV", "start": "2026-10-05", "end": "2026-06-17",
           "layers": {"GCOV": ["HHHH", "HHHV", "HHHH"], "GUNW": ["unwrappedPhase"], "SME2": []}}
    with pytest.raises(NisarConfigError) as err:
        parse_nisar_config(bad, today=TODAY)
    text = "\n".join(err.value.problems)
    for fragment in ["unknown keys ['product']", "unknown product 'GUNW'", "unsupported ['HHHV']",
                     "duplicated ['HHHH']", "nisar.layers.SME2 must be a non-empty list",
                     "start 2026-10-05 is after end 2026-06-17"]:
        assert fragment in text, fragment


def test_config_rejects_future_end_huge_ranges_and_no_products() -> None:
    with pytest.raises(NisarConfigError, match="in the future"):
        parse_nisar_config(dict(GCOV_BLOCK, end="2026-10-12"), today=TODAY)
    with pytest.raises(NisarConfigError, match="split ranges longer than"):
        parse_nisar_config(dict(GCOV_BLOCK, start="2024-12-07"), today=TODAY)
    with pytest.raises(NisarConfigError, match="must map one or more of"):
        parse_nisar_config(dict(GCOV_BLOCK, layers={}), today=TODAY)


def test_example_nisar_block_in_template_is_valid() -> None:
    text = template_text()
    lines = []
    for line in text[text.index("# nisar:"):].splitlines():
        if not line.startswith("#"):
            break
        lines.append(line[2:] if line.startswith("# ") else line[1:])
    cfg = parse_nisar_config(yaml.safe_load("\n".join(lines))["nisar"], today=TODAY)
    assert set(cfg.layers) == {"GCOV", "SME2"}


def test_nisar_command_needs_the_block_and_a_cache() -> None:
    cfg = {"aoi": {"aoi": "/a.shp"}, "paths": {"output_dir": "/out"},
           "raster": {"target_res": 10, "resampling_method": "bilinear"}}
    with pytest.raises(ConfigError) as err:
        validate_config(cfg, "nisar")
    text = "\n".join(err.value.problems)
    assert "nisar: missing" in text
    assert "paths.cache_dir: required because NISAR subsets are cached" in text


# ---------------------------------------------------------------- catalog


def test_scene_names_of_both_products_are_parsed() -> None:
    scene = parse_scene(NAME, URL.format(NAME), "provisional")
    assert (scene.track, scene.direction, scene.frame, scene.mode, scene.polarization) == (
        "071", "descending", "064", "4005", "DHDH")
    assert scene.start.isoformat() == "2026-10-05T03:09:53+00:00"
    assert scene.stem == "20261005T030953_T071D_F064"
    soil = parse_scene(SM_NAME, URL.format(SM_NAME), "beta")
    assert (soil.release, soil.track, soil.stem) == ("beta", "172", "20251216T031815_T172D_F064")
    with pytest.raises(NisarError, match="Unrecognised"):
        parse_scene("NISAR_L2_PR_GCOV_bad", "https://x", "provisional")
    with pytest.raises(NisarError, match="Unknown NISAR release"):
        parse_scene(NAME, "https://x", "validated")


def test_preferred_release_then_latest_processing_is_kept() -> None:
    old = parse_scene(NAME, "a", "provisional")
    newer = parse_scene(NAME.replace("P05023", "P05024"), "b", "provisional")
    recount = parse_scene(NAME.replace("_001", "_002"), "c", "provisional")
    beta = parse_scene(NAME.replace("P05023", "X05009"), "d", "beta")  # a higher CRID, but pre-calibration
    other = parse_scene(FAR_NAME.replace("P05023", "X05009"), "e", "beta")
    kept, superseded = latest_processing([beta, old, newer, recount, other])
    assert [s.id for s in kept] == [newer.id, other.id]
    assert superseded == sorted([old.id, recount.id, beta.id])


def test_data_url_takes_the_public_https_file_only() -> None:
    rel = catalog.DATA_REL
    entry = {"title": NAME, "links": [
        {"rel": rel, "href": URL.format(NAME)},
        {"rel": rel, "href": f"https://x/NISAR-JPL-PRIVATE-DATA/{NAME}/{NAME}.h5"},
        {"rel": rel, "href": f"https://x/{NAME}/{NAME}_QA_STATS.h5"},
        {"rel": "http://esipfed.org/ns/fedsearch/1.1/s3#", "href": f"s3://b/{NAME}.h5"},
    ]}
    assert data_url(entry) == URL.format(NAME)
    with pytest.raises(NisarError, match="Expected one HTTPS download link"):
        data_url({"title": NAME, "links": []})


def test_search_pages_through_both_releases(monkeypatch) -> None:
    calls = []

    class Page:
        def __init__(self, names, after):
            self.names, self.headers = names, ({"CMR-Search-After": after} if after else {})

        def raise_for_status(self):
            pass

        def json(self):
            return {"feed": {"entry": [
                {"title": n, "links": [{"rel": catalog.DATA_REL, "href": URL.format(n)}]} for n in self.names
            ]}}

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append((params["short_name"], headers))
        if params["short_name"].endswith("BETA_V1"):
            return Page([FAR_NAME.replace("P05023", "X05009")], None)
        return Page([NAME], "token") if not headers else Page([], None)

    monkeypatch.setattr(earthdata, "PAGE_SIZE", 1)
    monkeypatch.setattr(earthdata.requests, "get", fake_get)
    scenes, search = catalog.search_scenes("GCOV", (-121, 48, -120, 49), date(2025, 12, 7), date(2025, 12, 20))
    assert [(s.id[:16], s.release) for s in scenes] == [("NISAR_L2_PR_GCOV", "provisional"),
                                                         ("NISAR_L2_PR_GCOV", "beta")]
    assert search["granules_found"] == {"NISAR_L2_GCOV_PROVISIONAL_V1": 1, "NISAR_L2_GCOV_BETA_V1": 1}
    assert calls == [("NISAR_L2_GCOV_PROVISIONAL_V1", {}),
                     ("NISAR_L2_GCOV_PROVISIONAL_V1", {"CMR-Search-After": "token"}),
                     ("NISAR_L2_GCOV_BETA_V1", {})]
    assert search["query"]["temporal"] == "2025-12-07T00:00:00Z,2025-12-20T23:59:59Z"


def test_earthdata_credentials(tmp_path) -> None:
    session, headers = earthdata_session({"EARTHDATA_TOKEN": "abc"})
    assert headers == {"Authorization": "Bearer abc"}

    netrc_file = tmp_path / ".netrc"
    netrc_file.write_text(f"machine {EARTHDATA_HOST} login me password secret\n")
    netrc_file.chmod(0o600)
    session, headers = earthdata_session({}, netrc_path=netrc_file)
    assert headers == {}
    redirect = requests.Response()
    redirect.request = requests.Request("GET", "https://nisar.asf.example/f.h5").prepare()
    request = requests.Request("GET", f"https://{EARTHDATA_HOST}/oauth/authorize").prepare()
    session.rebuild_auth(request, redirect)
    assert request.headers["Authorization"].startswith("Basic ")
    elsewhere = requests.Request("GET", "https://signed.example/file.h5").prepare()
    session.rebuild_auth(elsewhere, redirect)
    assert "Authorization" not in elsewhere.headers

    with pytest.raises(EarthdataAuthError, match="none were found"):
        earthdata_session({}, netrc_path=tmp_path / "missing")


# ---------------------------------------------------------------- fake ASF


class FakeResponse:
    def __init__(self, status, content=b"", headers=None, url=""):
        self.status_code, self.content, self.headers, self.url = status, content, headers or {}, url

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeASF:
    """Serves files by range; the first request to each file is redirected to a signed link."""

    def __init__(self, files: dict[str, bytes]):
        self.files = files
        self.calls: list[tuple[str, dict]] = []
        self.expire_next = False

    def get(self, url, headers=None, timeout=None, stream=False):
        headers = dict(headers or {})
        self.calls.append((url, headers))
        if url.startswith("https://signed/"):
            if "Authorization" in headers:
                return FakeResponse(400)  # signed links refuse a second auth method
            if self.expire_next:
                self.expire_next = False
                return FakeResponse(403)
            body = self.files[url[len("https://signed/"):]]
        else:
            if headers.get("Authorization") != "Bearer good":
                return FakeResponse(200, b"<html>login</html>", {"Content-Type": "text/html"},
                                    f"https://{EARTHDATA_HOST}/oauth/authorize")
            body = self.files[url]
        start, end = (int(v) for v in headers["Range"].split("=")[1].split("-"))
        chunk = body[start:end + 1]
        signed = "https://signed/" + (url if not url.startswith("https://signed/") else url[len("https://signed/"):])
        return FakeResponse(206, chunk, {"Content-Range": f"bytes {start}-{end}/{len(body)}",
                                         "Content-Type": "application/octet-stream"}, signed)


def test_remote_file_reads_ranges_from_the_signed_link(monkeypatch) -> None:
    monkeypatch.setattr(earthdata.time, "sleep", lambda s: None)
    data = bytes(range(256)) * 400  # 102400 bytes
    asf = FakeASF({"https://asf/f.h5": data})
    remote = RemoteFile("https://asf/f.h5", asf, {"Authorization": "Bearer good"}, block_size=1000, max_blocks=50)
    assert remote.size == len(data)

    remote.seek(1500)
    assert remote.read(2600) == data[1500:4100]
    assert remote.range_requests == 1  # blocks 1-4 in one request
    remote.seek(2000)
    assert remote.read(1000) == data[2000:3000]
    assert remote.range_requests == 1  # served from cache
    assert all(url.startswith("https://signed/") and "Authorization" not in h for url, h in asf.calls[1:])

    asf.expire_next = True
    remote.seek(50000)
    assert remote.read(10) == data[50000:50010]
    remote.seek(-5, 2)
    assert remote.read(100) == data[-5:]


def test_refused_credentials_are_an_auth_error() -> None:
    asf = FakeASF({"https://asf/f.h5": b"x" * 10})
    with pytest.raises(EarthdataAuthError, match="refused access"):
        RemoteFile("https://asf/f.h5", asf, {"Authorization": "Bearer wrong"})


# ---------------------------------------------------------------- synthetic files


def _gcov(path: Path, x0: float = 499500.0, y0: float = 4100500.0, n: int = 100) -> Path:
    """A file laid out like a NISAR GCOV: 10 m UTM grid, NaN fill, mask 1-5 valid."""
    x = x0 + 5 + 10 * np.arange(n)
    y = y0 - 5 - 10 * np.arange(n)
    hh = np.full((n, n), HH, np.float32)
    hv = np.full((n, n), HV, np.float32)
    mask = np.full((n, n), 3, np.uint8)
    # Native columns 51-53 are the grid's first 30 m column: mark them invalid with a loud value.
    mask[:, 51:54] = 0
    hh[:, 51:54] = 1000.0
    mask[52, 60] = 255  # outside the image
    hh[52, 60] = 1000.0
    hh[55, 64] = np.nan
    with h5py.File(path, "w") as f:
        g = f.create_group(core.PRODUCTS["GCOV"].group)
        g["projection"] = np.uint32(32610)
        g["xCoordinates"], g["yCoordinates"] = x, y
        g["xCoordinateSpacing"], g["yCoordinateSpacing"] = 10.0, -10.0
        for name, data in (("HHHH", hh), ("HVHV", hv), ("mask", mask)):
            g.create_dataset(name, data=data, chunks=(32, 32), compression="gzip")
        g["HHHH"].attrs["_FillValue"] = np.float32(np.nan)
    return path


def _sme2(path: Path, withheld: bool = False) -> Path:
    """A file laid out like a NISAR SME2: 200 m EASE-Grid 2.0, -9999 fill, no DSG estimate.

    ``withheld`` fills the combined estimate everywhere, as NISAR does over snow.
    """
    res = 200.0
    west, south, east, north = transform_bounds(AOI_CRS, "EPSG:6933", *SM_AOI_BOUNDS)
    x = np.arange(west - 1000, east + 1000, res) + res / 2
    y = np.arange(north + 1000, south - 1000, -res) - res / 2
    shape = (len(y), len(x))
    combined = np.full(shape, -9999.0 if withheld else COMBINED, np.float32)
    tsr = np.full(shape, TSR, np.float32)
    tsr[:, x < (west + east) / 2] = -9999.0  # no TSR retrieval over the western half
    flag = np.full(shape, 15, np.int16)  # bit 0 set: not recommended
    flag[y < (south + north) / 2, :] = 14  # southern half recommended
    with h5py.File(path, "w") as f:
        g = f.create_group(core.PRODUCTS["SME2"].group)
        g["projection"] = np.uint32(6933)
        g["xCoordinates"], g["yCoordinates"] = x, y
        g["xCoordinateSpacing"], g["yCoordinateSpacing"] = res, -res
        for name, data in (("soilMoisture", combined), ("algorithmCandidates/TSR/soilMoisture", tsr),
                           ("retrievalQualityFlag", flag)):
            g.create_dataset(name, data=data, chunks=(8, 8), compression="gzip")
            g[name].attrs["_FillValue"] = data.dtype.type(-9999)
    return path


def test_native_window_keeps_values_and_blanks_invalid_pixels(tmp_path) -> None:
    with h5py.File(_gcov(tmp_path / "s.h5"), "r") as h5:
        subset = read_native_window(h5, "GCOV", ("HHHH", "HVHV"), AOI_BOUNDS, AOI_CRS)
        assert subset.crs == "EPSG:32610" and subset.absent == []
        row0, row1, col0, col1 = subset.window
        assert (row0, col0) == (49 - 2, 51 - 2)  # first grid row and column, less the pad
        assert subset.transform.c == 499500 + 10 * col0 and subset.transform.f == 4100500 - 10 * row0
        hh = subset.layers["HHHH"]
        assert not np.any(hh == 1000.0)
        assert np.all(hh[:, 2:5] == NODATA)  # mask 0
        assert hh[52 - row0, 60 - col0] == NODATA and hh[55 - row0, 64 - col0] == NODATA
        assert np.all(hh[hh != NODATA] == np.float32(HH))
        hv = subset.layers["HVHV"]
        assert hv[52 - row0, 60 - col0] == NODATA  # the mask blanks every layer
        assert np.all(hv[:, 5:][hv[:, 5:] != NODATA] == np.float32(HV))

        assert read_native_window(h5, "GCOV", ("HHHH",), (600000, 4099830, 600180, 4100010), AOI_CRS) is None
        missing = read_native_window(h5, "GCOV", ("VVVV", "HHHH"), AOI_BOUNDS, AOI_CRS)
        assert missing.absent == ["VVVV"] and list(missing.layers) == ["HHHH"]


def test_soil_moisture_window_reads_fill_and_the_recommended_bit(tmp_path) -> None:
    with h5py.File(_sme2(tmp_path / "sm.h5"), "r") as h5:
        subset = read_native_window(h5, "SME2", ("combined", "DSG", "TSR", "recommended"), SM_AOI_BOUNDS, AOI_CRS)
    assert subset.crs == "EPSG:6933" and subset.absent == ["DSG"]
    assert np.all(subset.layers["combined"] == np.float32(COMBINED))
    tsr = subset.layers["TSR"]
    assert np.any(tsr == NODATA) and np.all(tsr[tsr != NODATA] == np.float32(TSR))
    assert set(np.unique(subset.layers["recommended"])) == {0.0, 1.0}


# ---------------------------------------------------------------- end to end


def _aoi(path: Path, bounds) -> Path:
    gpd = pytest.importorskip("geopandas")
    from shapely.geometry import box

    gpd.GeoDataFrame({"id": [1]}, geometry=[box(*bounds)], crs=AOI_CRS).to_file(path)
    return path


@pytest.fixture
def nisar_env(tmp_path, monkeypatch):
    aoi = _aoi(tmp_path / "aoi.shp", AOI_BOUNDS)
    files = {
        URL.format(NAME): _gcov(tmp_path / "near.h5").read_bytes(),
        URL.format(FAR_NAME): _gcov(tmp_path / "far.h5", x0=650000.0).read_bytes(),
    }
    asf = FakeASF(files)
    scenes = [parse_scene(NAME, URL.format(NAME), "provisional"),
              parse_scene(FAR_NAME, URL.format(FAR_NAME), "provisional")]
    monkeypatch.setattr(core, "search_scenes", lambda *a: (scenes, {"granules_found": 2}))
    opened = []

    def open_scene(scene):
        opened.append(scene.id)
        return RemoteFile(scene.url, asf, {"Authorization": "Bearer good"})

    return aoi, grid_for_aoi(aoi, 30.0), tmp_path, open_scene, opened


def test_layers_land_on_the_grid_with_invalid_pixels_excluded(nisar_env) -> None:
    aoi, grid, tmp, open_scene, opened = nisar_env
    cfg = parse_nisar_config(GCOV_BLOCK, today=TODAY)
    [csv_path] = build_nisar_layers(cfg, aoi, tmp / "out", grid, tmp / "cache", open_scene=open_scene)
    out = tmp / "out" / "nisar_gcov"
    assert csv_path == out / "nisar_gcov_index.csv"

    rows = {r["scene"]: r for r in csv.DictReader(open(csv_path))}
    near, far = rows[NAME], rows[FAR_NAME]
    assert near["direction"] == "descending" and near["track"] == "071" and near["release"] == "provisional"
    assert float(near["HHHH_coverage"]) == pytest.approx(30 / 36)
    assert float(near["HHHH_mean"]) == pytest.approx(HH) and float(near["HVHV_mean"]) == pytest.approx(HV)
    assert near["HHHH_tif_path"] == "aligned_tif/HHHH/HHHH_20261005T030953_T071D_F064.tif"
    assert far["HHHH_tif_path"] == "" and far["note"] == "scene does not reach the AOI"

    for key in ("HHHH_tif_path", "HHHH_asc_path", "HVHV_tif_path", "HVHV_asc_path"):
        check_on_grid(out / near[key], grid)
    with rasterio.open(out / near["HHHH_tif_path"]) as src:
        hh = src.read(1)
    assert np.all(hh[:, 0] == NODATA)  # the column NISAR marks invalid
    assert np.allclose(hh[:, 1:], HH)  # the 255 and NaN pixels did not leak into their cells

    manifest = json.loads((out / "nisar_manifest.json").read_text())
    assert manifest["scenes"] == 2 and manifest["scenes_with_data"] == 1 and manifest["downloaded"] == 2
    assert manifest["template"] == grid.as_dict() and manifest["resampling"] == "average"
    near_record = next(r for r in manifest["records"] if r["scene"] == NAME)
    assert 0 < near_record["bytes_fetched"] <= near_record["file_bytes"]


def test_soil_moisture_lands_on_the_grid_unsmoothed(tmp_path, monkeypatch) -> None:
    aoi = _aoi(tmp_path / "aoi.shp", SM_AOI_BOUNDS)
    grid = grid_for_aoi(aoi, 100.0)
    scene = parse_scene(SM_NAME, URL.format(SM_NAME), "beta")
    asf = FakeASF({scene.url: _sme2(tmp_path / "sm.h5").read_bytes()})
    monkeypatch.setattr(core, "search_scenes", lambda *a: ([scene], {}))
    [csv_path] = build_nisar_layers(
        parse_nisar_config(SM_BLOCK, today=TODAY), aoi, tmp_path / "out", grid, tmp_path / "cache",
        open_scene=lambda s: RemoteFile(s.url, asf, {"Authorization": "Bearer good"}),
    )
    out = tmp_path / "out" / "nisar_sme2"
    [row] = list(csv.DictReader(open(csv_path)))
    assert row["release"] == "beta" and row["note"] == "scene lacks DSG" and row["DSG_tif_path"] == ""
    assert float(row["combined_coverage"]) == 1.0 and float(row["combined_mean"]) == pytest.approx(COMBINED)
    assert 0.3 < float(row["TSR_coverage"]) < 0.7 and float(row["TSR_mean"]) == pytest.approx(TSR)
    with rasterio.open(out / row["recommended_tif_path"]) as src:
        recommended = src.read(1)
    assert set(np.unique(recommended)) == {0.0, 1.0}  # nearest keeps the flag a flag
    for layer in ("combined", "TSR", "recommended"):
        check_on_grid(out / row[f"{layer}_tif_path"], grid)
    manifest = json.loads((out / "nisar_manifest.json").read_text())
    assert manifest["resampling"] == "nearest" and manifest["releases_used"] == {"beta": 1}


def test_a_layer_without_aoi_data_is_noted(tmp_path, monkeypatch) -> None:
    aoi = _aoi(tmp_path / "aoi.shp", SM_AOI_BOUNDS)
    scene = parse_scene(SM_NAME, URL.format(SM_NAME), "beta")
    asf = FakeASF({scene.url: _sme2(tmp_path / "sm.h5", withheld=True).read_bytes()})
    monkeypatch.setattr(core, "search_scenes", lambda *a: ([scene], {}))
    [csv_path] = build_nisar_layers(
        parse_nisar_config(SM_BLOCK, today=TODAY), aoi, tmp_path / "out", grid_for_aoi(aoi, 100.0),
        tmp_path / "cache", open_scene=lambda s: RemoteFile(s.url, asf, {"Authorization": "Bearer good"}),
    )
    [row] = list(csv.DictReader(open(csv_path)))
    assert row["note"] == "scene lacks DSG; no AOI data: combined"
    assert row["combined_tif_path"] == "" and float(row["combined_coverage"]) == 0.0 and row["TSR_tif_path"]


def test_cached_subsets_are_reused_without_reading_or_credentials(nisar_env) -> None:
    aoi, grid, tmp, open_scene, opened = nisar_env
    cfg = parse_nisar_config(GCOV_BLOCK, today=TODAY)
    build_nisar_layers(cfg, aoi, tmp / "out", grid, tmp / "cache", open_scene=open_scene)
    assert len(opened) == 2

    def no_reads(scene):
        raise AssertionError("cached scene was read again")

    build_nisar_layers(cfg, aoi, tmp / "again", grid, tmp / "cache", open_scene=no_reads)
    manifest = json.loads((tmp / "again" / "nisar_gcov" / "nisar_manifest.json").read_text())
    assert manifest["reused_from_cache"] == 2

    build_nisar_layers(cfg, aoi, tmp / "fresh", grid, tmp / "cache", refresh=True, open_scene=open_scene)
    assert len(opened) == 4


def test_no_scene_with_aoi_data_is_an_error(nisar_env, monkeypatch) -> None:
    aoi, grid, tmp, open_scene, opened = nisar_env
    far = [parse_scene(FAR_NAME, URL.format(FAR_NAME), "provisional")]
    monkeypatch.setattr(core, "search_scenes", lambda *a: (far, {}))
    with pytest.raises(NisarError, match="None of the 1 GCOV scenes has valid pixels"):
        build_nisar_layers(parse_nisar_config(GCOV_BLOCK, today=TODAY), aoi, tmp / "out", grid, tmp / "cache",
                           open_scene=open_scene)

    monkeypatch.setattr(core, "search_scenes", lambda *a: ([], {}))
    with pytest.raises(NisarError, match="No NISAR GCOV scenes"):
        build_nisar_layers(parse_nisar_config(GCOV_BLOCK, today=TODAY), aoi, tmp / "out", grid, tmp / "cache",
                           open_scene=open_scene)
