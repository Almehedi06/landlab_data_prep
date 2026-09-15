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

from landslide_data_prep import dem
from landslide_data_prep.dem import MissingApiKeyError, fetch_dem, resolve_api_key

KEY = "0123456789abcdef0123456789abcdef"
BMI = {"source": "bmi-topography", "dem_type": "USGS10m", "buffer_deg": 0.01, "api_key": KEY}


def test_key_order_matches_the_standalone_script() -> None:
    cfg = {"api_key": "from-config"}
    assert resolve_api_key(cfg, {"USGS_TOPO_API_KEY": "usgs", "OPENTOPOGRAPHY_API_KEY": "ot"}) == "usgs"
    assert resolve_api_key(cfg, {"OPENTOPOGRAPHY_API_KEY": " ot "}) == "ot"
    assert resolve_api_key(cfg, {}) == "from-config"


def test_missing_key_is_an_error_not_the_demo_key() -> None:
    with pytest.raises(MissingApiKeyError, match="dem.api_key"):
        resolve_api_key({"api_key": "  "}, {})


def test_missing_local_dem_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="Local DEM not found"):
        fetch_dem(tmp_path / "aoi.shp", {"source": "local", "path": str(tmp_path / "missing.tif")}, None)


def _write_dem(path: Path) -> None:
    with rasterio.open(
        path, "w", driver="GTiff", width=4, height=4, count=1, dtype="float32",
        crs="EPSG:32610", transform=from_origin(500000, 4100000, 30, 30), nodata=-9999.0,
    ) as dst:
        dst.write(np.ones((4, 4), np.float32), 1)


def _aoi(tmp_path: Path) -> Path:
    gpd = pytest.importorskip("geopandas")
    from shapely.geometry import box

    path = tmp_path / "aoi.shp"
    gpd.GeoDataFrame({"id": [1]}, geometry=[box(-120.4, 37.0, -120.3, 37.1)], crs="EPSG:4326").to_file(path)
    return path


class FakeTopography:
    outcomes: list = []
    seen: list = []

    def __init__(self, **kwargs) -> None:
        FakeTopography.seen.append(kwargs)

    def fetch(self) -> str:
        outcome = FakeTopography.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome()


@pytest.fixture
def fake_bmi(monkeypatch):
    FakeTopography.outcomes, FakeTopography.seen = [], []
    monkeypatch.setattr(dem, "Topography", FakeTopography)
    monkeypatch.delenv("USGS_TOPO_API_KEY", raising=False)
    monkeypatch.delenv("OPENTOPOGRAPHY_API_KEY", raising=False)
    return FakeTopography


def test_empty_cached_dem_is_replaced_by_a_fresh_download(tmp_path: Path, fake_bmi) -> None:
    cached = tmp_path / "cache" / "USGS10m_bbox.tif"
    cached.parent.mkdir()

    def empty() -> str:
        cached.write_bytes(b"")
        return str(cached)

    def good() -> str:
        _write_dem(cached)
        return str(cached)

    fake_bmi.outcomes = [empty, good]
    path = fetch_dem(_aoi(tmp_path), BMI, tmp_path / "cache")
    assert path == cached and path.stat().st_size > 0
    assert fake_bmi.seen[0]["api_key"] == KEY and fake_bmi.seen[0]["output_format"] == "GTiff"


def test_unusable_dem_twice_is_an_error(tmp_path: Path, fake_bmi) -> None:
    cached = tmp_path / "USGS10m_bbox.tif"

    def empty() -> str:
        cached.write_bytes(b"")
        return str(cached)

    fake_bmi.outcomes = [empty, empty]
    with pytest.raises(RuntimeError, match="unusable file twice"):
        fetch_dem(_aoi(tmp_path), BMI, tmp_path)


def test_download_errors_never_show_the_key(tmp_path: Path, fake_bmi) -> None:
    fake_bmi.outcomes = [RuntimeError(f"401 for https://portal.opentopography.org/API/usgsdem?API_Key={KEY}&south=1")]
    with pytest.raises(RuntimeError) as err:
        fetch_dem(_aoi(tmp_path), BMI, tmp_path)
    assert KEY not in str(err.value) and "[REDACTED]" in str(err.value)
