from __future__ import annotations

import csv
from datetime import date
import io
import json
import logging
from pathlib import Path
import sys
import zipfile

import numpy as np
import pytest
import rasterio
import requests
from rasterio.transform import from_origin
from rasterio.warp import transform_bounds
import yaml

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from landlab_data_prep.analysis_grid import check_on_grid, grid_for_aoi
from landlab_data_prep.config import ConfigError, validate_config
from landlab_data_prep.prism import core
from landlab_data_prep.prism.config import PrismConfigError, parse_prism_config
from landlab_data_prep.prism.core import PrismError, build_prism_forcing, parse_release

AOI_CRS = "EPSG:32610"
AOI_BOUNDS = (500010.0, 4099830.0, 500190.0, 4100010.0)
VALUES = {"ppt": 12.5, "tmin": -2.0, "tmax": 3.0}
BLOCK = {"start": "2025-12-07", "end": "2025-12-08", "variables": ["ppt", "tmin", "tmax"], "resolution": "800m"}


# ---------------------------------------------------------------- config


def test_parse_valid_block() -> None:
    cfg = parse_prism_config(dict(BLOCK, start=date(2025, 12, 7)), today=date(2026, 9, 18))
    assert cfg.days == [date(2025, 12, 7), date(2025, 12, 8)]
    assert cfg.variables == ("ppt", "tmin", "tmax") and cfg.resolution == "800m"


def test_config_reports_every_problem_at_once() -> None:
    bad = {"start": "2025-12-20", "end": "2025-12-07", "variables": ["ppt", "tmean"], "resolution": "250m", "region": "us"}
    with pytest.raises(PrismConfigError) as err:
        parse_prism_config(bad, today=date(2026, 9, 18))
    text = "\n".join(err.value.problems)
    for fragment in ["unknown keys ['region']", "start 2025-12-20 is after end 2025-12-07", "unsupported ['tmean']", "resolution='250m'"]:
        assert fragment in text, fragment


def test_config_rejects_future_end_and_huge_ranges() -> None:
    with pytest.raises(PrismConfigError, match="not in the past"):
        parse_prism_config(dict(BLOCK, end="2026-09-18"), today=date(2026, 9, 18))
    with pytest.raises(PrismConfigError, match="split ranges longer than"):
        parse_prism_config(dict(BLOCK, start="2015-12-07", end="2025-12-08"), today=date(2026, 9, 18))


def test_example_prism_block_in_template_is_valid() -> None:
    text = (ROOT / "config" / "base.example.yaml").read_text()
    lines = []
    for line in text[text.index("# prism:"):].splitlines():
        if not line.startswith("#"):
            break
        lines.append(line[2:] if line.startswith("# ") else line[1:])
    parse_prism_config(yaml.safe_load("\n".join(lines))["prism"])


def test_prism_command_needs_the_block_and_a_cache() -> None:
    cfg = {"aoi": {"aoi": "/a.shp"}, "paths": {"output_dir": "/out"}, "raster": {"target_res": 10, "resampling_method": "bilinear"}}
    with pytest.raises(ConfigError) as err:
        validate_config(cfg, "prism")
    text = "\n".join(err.value.problems)
    assert "prism: missing" in text and "paths.cache_dir: required because PRISM grids download" in text


def test_release_metadata_format() -> None:
    release = parse_release(["2025-12-07", "2026-06-25", "ppt", "8", "https://x"], "ppt")
    assert (release.date, release.number, release.tag) == ("2026-06-25", "8", "r008_2026-06-25")
    with pytest.raises(PrismError, match="Unexpected PRISM release metadata"):
        parse_release({"releaseDate": "2026-06-25"}, "ppt")


# ---------------------------------------------------------------- fake PRISM service


def _write_aoi(path: Path) -> Path:
    gpd = pytest.importorskip("geopandas")
    from shapely.geometry import box

    gpd.GeoDataFrame({"id": [1]}, geometry=[box(*AOI_BOUNDS)], crs=AOI_CRS).to_file(path)
    return path


def _prism_zip(variable: str, day: str, fill: float | None = None) -> bytes:
    """A zip shaped like PRISM's: one GeoTIFF in NAD83 geographic plus an info file."""
    west, south, east, north = transform_bounds(AOI_CRS, "EPSG:4269", *AOI_BOUNDS)
    res = 0.008333
    width, height = int((east - west) / res) + 6, int((north - south) / res) + 6
    memfile = io.BytesIO()
    with rasterio.MemoryFile() as mem:
        with mem.open(driver="GTiff", width=width, height=height, count=1, dtype="float32",
                      crs="EPSG:4269", transform=from_origin(west - 3 * res, north + 3 * res, res, res), nodata=-9999.0) as dst:
            dst.write(np.full((height, width), VALUES[variable] if fill is None else fill, np.float32), 1)
        tif_bytes = mem.read()
    with zipfile.ZipFile(memfile, "w") as zf:
        zf.writestr(f"prism_{variable}_us_30s_{day}.tif", tif_bytes)
        zf.writestr(f"prism_{variable}_us_30s_{day}.info.txt", "PRISM_DATASET_TYPE: an91/r2112\nPRISM_DATASET_RELEASE_NUMBER: 8\n")
    return memfile.getvalue()


class FakeResponse:
    def __init__(self, body=b"", payload=None, status=200):
        self.body, self.payload, self.status_code = body, payload, status
        self.headers = {"Content-Length": str(len(body))} if body else {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error", response=self)

    def json(self):
        return self.payload

    def iter_content(self, chunk_size):
        yield self.body


class FakePrism:
    def __init__(self):
        self.release_number = "8"
        self.release_down = False
        self.fill = None  # replaces every grid value, e.g. PRISM's nodata
        self.body = None  # replaces the zip, e.g. a refusal message
        self.data_calls: list[str] = []

    def get(self, url, **kwargs):
        parts = url.split("?")[0].rstrip("/").split("/")
        day, variable = parts[-1], parts[-2]
        if "/releaseDate/" in url:
            if self.release_down:
                raise requests.ConnectionError("release service unreachable")
            return FakeResponse(payload=[f"{day[:4]}-{day[4:6]}-{day[6:]}", "2026-06-25", variable, self.release_number, url])
        self.data_calls.append(url)
        return FakeResponse(body=self.body if self.body is not None else _prism_zip(variable, day, self.fill))


@pytest.fixture
def prism_env(tmp_path, monkeypatch):
    fake = FakePrism()
    monkeypatch.setattr(requests, "get", fake.get)
    monkeypatch.setattr(core, "POLITE_DELAY_SECONDS", 0)
    aoi = _write_aoi(tmp_path / "aoi.shp")
    return fake, aoi, grid_for_aoi(aoi, 30.0), tmp_path


def _run(cfg_block, env, out="out"):
    fake, aoi, grid, tmp = env
    return build_prism_forcing(parse_prism_config(cfg_block), aoi, tmp / out, grid, tmp / "cache")


def test_forcing_lands_on_the_grid_in_the_layout_landlab_debrisflow_reads(prism_env) -> None:
    fake, aoi, grid, tmp = prism_env
    csv_path = _run(BLOCK, prism_env)
    out = tmp / "out"
    assert csv_path == out / "forcing_daily_prism.csv"

    rows = list(csv.DictReader(open(csv_path)))
    assert [r["datetime"] for r in rows] == ["2025-12-07", "2025-12-08"]
    assert rows[0]["ppt_asc_path"] == "asc/ppt/precip_20251207.asc"
    assert rows[0]["tmin_asc_path"] == "asc/tmin/tmin_20251207.asc"
    assert rows[0]["tmax_tif_path"] == "aligned_tif/tmax/tmax_20251207.tif"
    for row in rows:
        assert float(row["precip_mm"]) == pytest.approx(12.5)
        assert float(row["tmin_c"]) == pytest.approx(-2.0)
        assert float(row["tmax_c"]) == pytest.approx(3.0)
        assert row["ppt_release_number"] == "8" and row["ppt_release_date"] == "2026-06-25"
        for variable in ("ppt", "tmin", "tmax"):
            check_on_grid(out / row[f"{variable}_tif_path"], grid)
            check_on_grid(out / row[f"{variable}_asc_path"], grid)

    manifest = json.loads((out / "prism_manifest.json").read_text())
    assert manifest["downloads"] == 6 and manifest["reused_from_cache"] == 0
    assert manifest["template"] == grid.as_dict() and manifest["resolution"] == "800m"
    assert manifest["records"][0]["dataset"]["PRISM_DATASET_TYPE"] == "an91/r2112"
    assert len(fake.data_calls) == 6


def test_unchanged_releases_are_reused_and_revised_ones_downloaded(prism_env) -> None:
    fake, *_ = prism_env
    _run(BLOCK, prism_env)
    _run(BLOCK, prism_env, out="again")
    assert len(fake.data_calls) == 6  # nothing downloaded twice

    fake.release_number = "9"
    _run(BLOCK, prism_env, out="revised")
    assert len(fake.data_calls) == 12


def test_unreachable_release_service_uses_cache_loudly_or_fails(prism_env, caplog) -> None:
    fake, *_ = prism_env
    fake.release_down = True
    with pytest.raises(PrismError, match="nothing is cached"):
        _run(BLOCK, prism_env)

    fake.release_down = False
    _run(BLOCK, prism_env)
    fake.release_down = True
    with caplog.at_level(logging.WARNING):
        csv_path = _run(BLOCK, prism_env, out="offline")
    assert "Could not check the PRISM release" in caplog.text
    manifest = json.loads((csv_path.parent / "prism_manifest.json").read_text())
    assert manifest["unverified_releases"] == 6


def test_missing_prism_grid_is_a_clear_error(prism_env, monkeypatch) -> None:
    fake, *_ = prism_env

    def not_found(url, **kwargs):
        if "/releaseDate/" in url:
            return fake.get(url)
        return FakeResponse(status=404)

    monkeypatch.setattr(requests, "get", not_found)
    with pytest.raises(PrismError, match="404"):
        _run(dict(BLOCK, variables=["ppt"]), prism_env)


def test_a_refusal_message_is_an_error_and_is_not_cached(prism_env) -> None:
    fake, *_ = prism_env
    fake.body = b"You have tried to download the file more than twice in one day"
    with pytest.raises(PrismError, match="did not return a zip.*more than twice"):
        _run(dict(BLOCK, variables=["ppt"]), prism_env)
    assert not list((prism_env[3] / "cache").rglob("*.zip"))

    fake.body = None
    _run(dict(BLOCK, variables=["ppt"]), prism_env)  # the next run downloads normally
    assert len(fake.data_calls) == 3


def test_aoi_outside_prism_coverage_is_an_error(prism_env) -> None:
    fake, *_ = prism_env
    fake.fill = -9999.0
    with pytest.raises(PrismError, match="no valid pixels.*conterminous US"):
        _run(dict(BLOCK, variables=["tmin"]), prism_env)


def test_command_uses_the_shared_config(prism_env) -> None:
    from landlab_data_prep.prism import cli_run

    fake, aoi, grid, tmp = prism_env
    cfg = {
        "aoi": {"aoi": str(aoi)},
        "paths": {"output_dir": str(tmp / "outputs"), "cache_dir": str(tmp / "cache")},
        "raster": {"target_res": 30, "resampling_method": "bilinear"},
        "prism": dict(BLOCK, variables=["ppt"]),
    }
    config_path = tmp / "config.yaml"
    config_path.write_text(yaml.safe_dump(cfg))
    assert cli_run.main(["--config", str(config_path)]) == 0
    assert (tmp / "outputs" / "prism_forcing" / "forcing_daily_prism.csv").is_file()
