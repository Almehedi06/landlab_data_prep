from __future__ import annotations

import copy
from datetime import date, datetime, timezone
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from rasterio.warp import transform_bounds
import yaml

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from landslide_data_prep.analysis_grid import check_on_grid, grid_for_aoi, snap_grid
from landslide_data_prep.remote_sensing import catalog, composite
from landslide_data_prep.remote_sensing.catalog import (
    EarthdataAuthError,
    Scene,
    earthdata_gdal_options,
    per_thread_options_factory,
    scenes_from_items,
)
from landslide_data_prep.remote_sensing.composite import check_scale, decode_vi, group_by_solar_day
from landslide_data_prep.remote_sensing.config import RemoteSensingConfigError, parse_remote_sensing_config
from landslide_data_prep.remote_sensing.core import (
    MANIFEST_NAME,
    EmptyWindowError,
    build_hls_products,
    product_specs,
)
from landslide_data_prep.remote_sensing.indices import difference, dnbr, rdnbr
from landslide_data_prep.remote_sensing.qa import clear_mask

UTC = timezone.utc
INDICES = ("NBR", "NDVI", "NDMI")

VALID_BLOCK = {
    "windows": {
        "pre": {"start": date(2017, 6, 1), "end": "2017-09-30"},
        "post": {"start": "2018-06-01", "end": date(2018, 9, 30)},
    },
    "indices": list(INDICES),
    "cloud_max": 60,
    "compositing": "median",
}


def _block(**overrides) -> dict:
    block = copy.deepcopy(VALID_BLOCK)
    block.update(overrides)
    return block


# ---------------------------------------------------------------- change metrics


def test_difference_propagates_missing_pixels() -> None:
    out = difference(np.array([0.8, np.nan, 0.5]), np.array([0.3, 0.2, np.nan]))
    assert np.isclose(out[0], 0.5)
    assert np.isnan(out[1]) and np.isnan(out[2])


def test_dnbr_uses_x1000_convention() -> None:
    assert np.isclose(dnbr([0.6], [0.1])[0], 500.0)


def test_rdnbr_matches_miller_thode_and_floors_near_zero_pre() -> None:
    # dNBR = 1000 * (0.25 - -0.25) = 500; sqrt(0.25) = 0.5
    assert np.isclose(rdnbr([0.25], [-0.25])[0], 1000.0)
    # |NBR_pre| = 0 is floored at 0.001 instead of dividing by zero
    assert np.isclose(rdnbr([0.0], [-0.1])[0], 100.0 / np.sqrt(0.001), rtol=1e-4)
    assert np.isnan(rdnbr([np.nan], [0.1])[0])


def test_change_metrics_reject_shape_mismatch() -> None:
    with pytest.raises(ValueError, match="Shape mismatch"):
        difference(np.zeros(3), np.zeros(4))


# ---------------------------------------------------------------- Fmask


@pytest.mark.parametrize("bit", [1, 2, 3, 4])
def test_clear_mask_rejects_cloud_adjacent_shadow_snow(bit: int) -> None:
    assert not clear_mask(np.array([1 << bit], dtype=np.uint8))[0]


def test_clear_mask_keeps_clear_water_unused_cirrus_and_low_medium_aerosol() -> None:
    values = np.array([0, 1 << 5, 1 << 0, 1 << 6, 2 << 6], dtype=np.uint8)
    assert clear_mask(values).all()


def test_clear_mask_rejects_high_aerosol_and_fill() -> None:
    assert not clear_mask(np.array([3 << 6], dtype=np.uint8))[0]
    assert not clear_mask(np.array([255], dtype=np.uint8))[0]
    assert not clear_mask(np.array([0], dtype=np.uint8), fill=0)[0]


def test_clear_mask_user_guide_appendix_example() -> None:
    # QA 100 = water + adjacent to cloud + low aerosol (HLS guide, Appendix A)
    assert not clear_mask(np.array([100], dtype=np.uint8))[0]


def test_clear_mask_refuses_float_input() -> None:
    with pytest.raises(TypeError):
        clear_mask(np.array([0.0]))


# ---------------------------------------------------------------- config


def test_parse_valid_config_accepts_yaml_dates_and_strings() -> None:
    cfg = parse_remote_sensing_config(_block())
    assert cfg.pre.start == date(2017, 6, 1) and cfg.pre.end == date(2017, 9, 30)
    assert cfg.post.start == date(2018, 6, 1) and cfg.post.end == date(2018, 9, 30)
    assert cfg.indices == INDICES
    assert cfg.cloud_max == 60.0
    assert cfg.resampling is None


def test_config_reports_every_problem_at_once() -> None:
    bad = {
        "windows": {
            "pre": {"start": "2017-09-30", "end": "2017-06-01"},
            "post": {"start": "2018-06-01"},
        },
        "indices": ["ndvi"],
        "cloud_max": 150,
        "compositing": "mean",
        "cloudmax": 60,
    }
    with pytest.raises(RemoteSensingConfigError) as err:
        parse_remote_sensing_config(bad)
    text = "\n".join(err.value.problems)
    for fragment in [
        "unknown keys ['cloudmax']",
        "start 2017-09-30 is after end 2017-06-01",
        "windows.post: missing required keys ['end']",
        "unsupported ['ndvi']",
        "between 0 and 100",
        "compositing='mean'",
    ]:
        assert fragment in text, fragment


def test_config_requires_pre_to_end_before_post() -> None:
    windows = {"pre": {"start": "2017-06-01", "end": "2018-07-01"}, "post": {"start": "2018-06-01", "end": "2018-09-30"}}
    with pytest.raises(RemoteSensingConfigError, match="pre must end before post starts"):
        parse_remote_sensing_config(_block(windows=windows))


def test_config_rejects_datetime_bool_and_missing_keys() -> None:
    windows = {"pre": {"start": datetime(2017, 6, 1, 10), "end": "2017-09-30"}, "post": VALID_BLOCK["windows"]["post"]}
    with pytest.raises(RemoteSensingConfigError) as err:
        parse_remote_sensing_config(_block(windows=windows, cloud_max=True))
    text = "\n".join(err.value.problems)
    assert "got a datetime" in text and "cloud_max must be a number" in text

    with pytest.raises(RemoteSensingConfigError, match="missing required keys"):
        parse_remote_sensing_config({})
    with pytest.raises(RemoteSensingConfigError, match="must be a mapping"):
        parse_remote_sensing_config(None)


def test_example_config_block_is_valid() -> None:
    """The commented example in the tracked template must stay parseable."""
    text = (ROOT / "config" / "base.example.yaml").read_text()
    start = text.index("# remote_sensing:")
    lines = []
    for line in text[start:].splitlines():
        if not line.startswith("#"):
            break
        lines.append(line[2:] if line.startswith("# ") else line[1:])
    parse_remote_sensing_config(yaml.safe_load("\n".join(lines))["remote_sensing"])


def test_product_specs_names_and_units() -> None:
    ndvi = {s.key: s for s in product_specs(parse_remote_sensing_config(_block(indices=["NDVI"])))}
    assert set(ndvi) == {"hls_ndvi_pre", "hls_ndvi_post", "hls_dndvi", "hls_clear_count_pre", "hls_clear_count_post"}
    assert ndvi["hls_clear_count_pre"].is_count and not ndvi["hls_dndvi"].is_count
    assert ndvi["hls_ndvi_pre"].field_name == "hls__ndvi_pre"

    nbr = {s.key: s for s in product_specs(parse_remote_sensing_config(_block(indices=["NBR"])))}
    assert nbr["hls_dnbr"].units == "unitless x1000"
    assert "hls_rdnbr" in nbr and "hls_dnbr" in nbr


# ---------------------------------------------------------------- catalog and auth


def _item(item_id: str, when: datetime, cloud_cover, assets=("Fmask", *INDICES), href_root="/data"):
    props = {} if cloud_cover is None else {"eo:cloud_cover": cloud_cover}
    return SimpleNamespace(
        id=item_id,
        datetime=when,
        properties=props,
        assets={name: SimpleNamespace(href=f"{href_root}/{item_id}/{name}.tif") for name in assets},
    )


def test_scenes_from_items_filters_cloud_dedupes_and_sorts() -> None:
    t1, t2 = datetime(2018, 7, 1, tzinfo=UTC), datetime(2018, 7, 11, tzinfo=UTC)
    items = [_item("b", t2, 10), _item("a", t1, 80), _item("c", t1, 5), _item("b", t2, 10)]
    scenes, stats = scenes_from_items(items, ["NDVI"], cloud_max=60)
    assert [s.id for s in scenes] == ["c", "b"]
    assert stats == {"items_found": 4, "items_unique": 3, "dropped_above_cloud_max": 1, "scenes_used": 2}
    assert set(scenes[0].hrefs) == {"Fmask", "NDVI"}


def test_scenes_from_items_fails_loudly_on_unusable_items() -> None:
    t = datetime(2018, 7, 1, tzinfo=UTC)
    items = [_item("no_cc", t, None), _item("no_ndmi", t, 5, assets=("Fmask", "NBR", "NDVI"))]
    with pytest.raises(ValueError, match="Unusable catalog items") as err:
        scenes_from_items(items, INDICES, cloud_max=60)
    assert "no 'eo:cloud_cover'" in str(err.value)
    assert "missing assets ['NDMI']" in str(err.value)


def test_token_auth_uses_bearer_header(tmp_path: Path) -> None:
    opts = earthdata_gdal_options({"EARTHDATA_TOKEN": "abc"}, netrc_path=tmp_path / "absent")
    assert opts["GDAL_HTTP_HEADERS"] == "Authorization: Bearer abc"
    assert per_thread_options_factory(opts, tmp_path)() == opts


def test_netrc_auth_gives_each_thread_its_own_cookie_jar(tmp_path: Path) -> None:
    netrc_file = tmp_path / "netrc"
    netrc_file.write_text("machine urs.earthdata.nasa.gov login user password secret\n")
    opts = earthdata_gdal_options({}, netrc_path=netrc_file)
    assert opts["GDAL_HTTP_NETRC"] == "YES" and opts["GDAL_HTTP_NETRC_FILE"] == str(netrc_file)
    thread_opts = per_thread_options_factory(opts, tmp_path)()
    assert thread_opts["GDAL_HTTP_COOKIEFILE"] == thread_opts["GDAL_HTTP_COOKIEJAR"]
    assert thread_opts["GDAL_HTTP_COOKIEJAR"].startswith(str(tmp_path))


def test_missing_credentials_raise_with_both_routes(tmp_path: Path) -> None:
    with pytest.raises(EarthdataAuthError, match="EARTHDATA_TOKEN"):
        earthdata_gdal_options({}, netrc_path=tmp_path / "absent")
    other = tmp_path / "netrc"
    other.write_text("machine example.com login u password p\n")
    with pytest.raises(EarthdataAuthError, match="urs.earthdata.nasa.gov"):
        earthdata_gdal_options({}, netrc_path=other)


# ---------------------------------------------------------------- grid, decoding, grouping


def test_decode_vi_scales_and_masks_fill() -> None:
    out = decode_vi(np.array([10000, -5000, -19999], dtype=np.int16), fill=-19999, scale=0.0001)
    assert np.allclose(out[:2], [1.0, -0.5])
    assert np.isnan(out[2])


def test_check_scale_catches_a_missing_scale_factor() -> None:
    with pytest.raises(ValueError, match="scale factor is wrong"):
        check_scale(np.array([5000.0, 6000.0, np.nan], dtype=np.float32), "unscaled")
    check_scale(np.array([0.5, -0.9, 1.2, np.nan], dtype=np.float32), "fine")


def test_solar_day_groups_one_pass_across_utc_midnight() -> None:
    # At longitude -150 local solar time is UTC-10, so both tiles fall on Jan 1.
    late = Scene("a", datetime(2018, 1, 1, 23, 50, tzinfo=UTC), 0.0, {})
    early = Scene("b", datetime(2018, 1, 2, 0, 10, tzinfo=UTC), 0.0, {})
    groups = group_by_solar_day([early, late], -150.0)
    assert len(groups) == 1
    assert groups[0][0] == date(2018, 1, 1)
    assert [s.id for s in groups[0][1]] == ["a", "b"]


def test_memory_guard_raises_before_reading(monkeypatch) -> None:
    monkeypatch.setattr(composite, "MAX_STACK_BYTES", 1)
    monkeypatch.setattr(composite, "read_scene", lambda *a, **k: pytest.fail("read before memory check"))
    scene = Scene("s", datetime(2018, 1, 1, tzinfo=UTC), 0.0, {"Fmask": "/nope", "NDVI": "/nope"})
    grid = snap_grid(AOI_BOUNDS, AOI_CRS, 30.0)
    with pytest.raises(MemoryError, match="Shorten the windows"):
        composite.composite_window([scene], ["NDVI"], grid, -120.0, dict)


# ---------------------------------------------------------------- synthetic HLS scenes

# 6x6 AOI at 30 m in UTM zone 10, close enough to zone 11 that a
# neighbouring-zone tile can cover the same ground, as at Montecito.
AOI_CRS = "EPSG:32610"
AOI_BOUNDS = (740010.0, 4099830.0, 740190.0, 4100010.0)
TILE_ORIGIN = (739980.0, 4100040.0)  # tile pixel (r+1, c+1) is grid pixel (r, c)
TILE_SHAPE = (10, 10)


def _write_raster(path: Path, data: np.ndarray, transform, crs: str, nodata, scale=None) -> None:
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=data.shape[1],
        height=data.shape[0],
        count=1,
        dtype=data.dtype,
        crs=crs,
        transform=transform,
        nodata=nodata,
    ) as dst:
        dst.write(data, 1)
        if scale is not None:
            dst.scales = (scale,)


def _write_aoi(path: Path, bounds=AOI_BOUNDS, crs: str = AOI_CRS) -> None:
    gpd = pytest.importorskip("geopandas")
    from shapely.geometry import box

    gpd.GeoDataFrame({"id": [1]}, geometry=[box(*bounds)], crs=crs).to_file(path, driver="ESRI Shapefile")


def _zone11_tile() -> tuple[tuple[float, float], tuple[int, int]]:
    left, bottom, right, top = transform_bounds(AOI_CRS, "EPSG:32611", *AOI_BOUNDS)
    pad = 300.0
    x0 = float(np.floor((left - pad) / 30.0) * 30.0)
    y1 = float(np.ceil((top + pad) / 30.0) * 30.0)
    width = int(np.ceil((right + pad - x0) / 30.0))
    height = int(np.ceil((y1 - (bottom - pad)) / 30.0))
    return (x0, y1), (height, width)


class SceneFactory:
    """Writes synthetic HLS VI scenes as local GeoTIFFs and serves them as a fake catalog."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.items: list[SimpleNamespace] = []
        self.calls: list[tuple] = []

    def add(
        self,
        scene_id: str,
        when: datetime,
        values: dict[str, float],
        *,
        cloud_cover: float = 10.0,
        fmask: dict[tuple[int, int], int] | None = None,
        ndvi_fill_at: tuple[tuple[int, int], ...] = (),
        crs: str = AOI_CRS,
        origin: tuple[float, float] = TILE_ORIGIN,
        shape: tuple[int, int] = TILE_SHAPE,
        declared_scale: float | None = None,
    ) -> None:
        folder = self.root / scene_id
        folder.mkdir(parents=True)
        transform = from_origin(origin[0], origin[1], 30.0, 30.0)

        hrefs = {"Fmask": folder / "Fmask.tif"}
        fm = np.zeros(shape, dtype=np.uint8)
        for (r, c), byte in (fmask or {}).items():
            fm[r + 1, c + 1] = byte
        _write_raster(hrefs["Fmask"], fm, transform, crs, nodata=255)

        for name in INDICES:
            raw = np.full(shape, int(round(values[name] * 10000)), dtype=np.int16)
            if name == "NDVI":
                for r, c in ndvi_fill_at:
                    raw[r + 1, c + 1] = -19999
            hrefs[name] = folder / f"{name}.tif"
            _write_raster(hrefs[name], raw, transform, crs, nodata=-19999, scale=declared_scale)

        self.items.append(
            SimpleNamespace(
                id=scene_id,
                datetime=when,
                properties={"eo:cloud_cover": cloud_cover},
                assets={k: SimpleNamespace(href=str(v)) for k, v in hrefs.items()},
            )
        )

    def search(self, bbox, start: date, end: date) -> list:
        self.calls.append((bbox, start, end))
        return [i for i in self.items if start <= i.datetime.date() <= end]


def _synthetic_scenes(root: Path) -> SceneFactory:
    def t(y, m, d, hh=18, mm=40, ss=0):
        return datetime(y, m, d, hh, mm, ss, tzinfo=UTC)

    f = SceneFactory(root)
    # Pre: three clear solar days. Day one is also seen by a zone-11 tile of the
    # same pass; counted twice, the NDVI median would be 0.75 instead of 0.7.
    f.add("P1", t(2017, 7, 1), {"NBR": 0.6, "NDVI": 0.8, "NDMI": 0.4})
    origin, shape = _zone11_tile()
    f.add("P1_zone11", t(2017, 7, 1, ss=5), {"NBR": 0.6, "NDVI": 0.8, "NDMI": 0.4},
          crs="EPSG:32611", origin=origin, shape=shape)
    f.add("P2", t(2017, 7, 11), {"NBR": 0.4, "NDVI": 0.6, "NDMI": 0.2}, fmask={(0, 0): 1 << 4})  # snow
    f.add("P3", t(2017, 7, 21), {"NBR": 0.5, "NDVI": 0.7, "NDMI": 0.3}, declared_scale=0.0001)
    # Post: Q2 is dropped by cloud_max (it would pull medians to 0.3), Q3 has
    # high aerosol at (5, 5), Q1 has an NDVI fill at (2, 3).
    f.add("Q1", t(2018, 7, 1), {"NBR": -0.2, "NDVI": 0.2, "NDMI": 0.0}, ndvi_fill_at=((2, 3),))
    f.add("Q2", t(2018, 7, 11), {"NBR": 0.9, "NDVI": 0.9, "NDMI": 0.9}, cloud_cover=90.0)
    f.add("Q3", t(2018, 7, 21), {"NBR": -0.1, "NDVI": 0.3, "NDMI": 0.05}, fmask={(5, 5): 3 << 6})
    return f


EXPECTED_KEYS = [
    "hls_nbr_pre", "hls_nbr_post",
    "hls_ndvi_pre", "hls_ndvi_post",
    "hls_ndmi_pre", "hls_ndmi_post",
    "hls_dnbr", "hls_rdnbr", "hls_dndvi", "hls_dndmi",
    "hls_clear_count_pre", "hls_clear_count_post",
]


def test_build_hls_products_end_to_end_offline(tmp_path: Path, monkeypatch) -> None:
    factory = _synthetic_scenes(tmp_path / "hls")
    monkeypatch.setattr(catalog, "search_hls_vi", factory.search)
    monkeypatch.setattr(catalog, "earthdata_gdal_options", lambda: pytest.fail("local files need no credentials"))
    aoi = tmp_path / "aoi.shp"
    _write_aoi(aoi)

    products = build_hls_products(
        parse_remote_sensing_config(_block()), aoi, tmp_path / "out", grid_for_aoi(aoi, 30.0),
        continuous_resampling="bilinear", max_workers=2,
    )
    assert [p.key for p in products] == EXPECTED_KEYS

    a = {}
    for product in products:
        with rasterio.open(product.path) as src:
            assert src.crs.to_string() == AOI_CRS
            assert tuple(src.transform)[:6] == (30.0, 0.0, 740010.0, 0.0, -30.0, 4100010.0)
            assert (src.height, src.width) == (6, 6)
            assert src.nodata == -9999.0
            assert src.tags()["native_resolution_m"] == "30"
            a[product.key] = src.read(1)

    tol = dict(atol=1e-5)
    # Pre: median over three solar days; the zone-11 copy of day one is fused, not a fourth day.
    assert np.isclose(a["hls_ndvi_pre"][3, 3], 0.7, **tol)
    assert np.isclose(a["hls_nbr_pre"][3, 3], 0.5, **tol)
    assert np.isclose(a["hls_ndmi_pre"][3, 3], 0.3, **tol)
    assert a["hls_clear_count_pre"][3, 3] == 3
    # Snow in P2 at (0, 0) leaves two days there.
    assert np.isclose(a["hls_ndvi_pre"][0, 0], 0.75, **tol)
    assert a["hls_clear_count_pre"][0, 0] == 2

    # Post: the cloudy scene is gone, so the median of 0.2 and 0.3 is 0.25.
    assert np.isclose(a["hls_ndvi_post"][3, 3], 0.25, **tol)
    assert np.isclose(a["hls_nbr_post"][3, 3], -0.15, **tol)
    assert a["hls_clear_count_post"][3, 3] == 2
    # High aerosol in Q3 at (5, 5): only Q1 remains.
    assert np.isclose(a["hls_ndvi_post"][5, 5], 0.2, **tol) and a["hls_clear_count_post"][5, 5] == 1
    # NDVI fill in Q1 at (2, 3) removes Q1 for every index, not just NDVI.
    assert np.isclose(a["hls_ndvi_post"][2, 3], 0.3, **tol)
    assert np.isclose(a["hls_nbr_post"][2, 3], -0.1, **tol)
    assert a["hls_clear_count_post"][2, 3] == 1

    # Change metrics on the native grid.
    assert np.isclose(a["hls_dndvi"][3, 3], 0.45, **tol)
    assert np.isclose(a["hls_dndmi"][3, 3], 0.275, **tol)
    assert np.isclose(a["hls_dnbr"][3, 3], 650.0, atol=1e-2)
    assert np.isclose(a["hls_rdnbr"][3, 3], 650.0 / np.sqrt(0.5), atol=1e-2)
    assert (a["hls_clear_count_post"] >= 1).all()

    manifest = json.loads((tmp_path / "out" / MANIFEST_NAME).read_text())
    assert manifest["native_resolution_m"] == 30.0
    assert manifest["windows"]["pre"]["solar_days"] == 3
    assert manifest["windows"]["pre"]["scenes_used"] == 4
    assert manifest["windows"]["post"]["dropped_above_cloud_max"] == 1
    assert "Q2" not in manifest["windows"]["post"]["scene_ids"]
    assert [p["key"] for p in manifest["products"]] == EXPECTED_KEYS
    assert {p["units"] for p in manifest["products"] if p["key"] == "hls_dnbr"} == {"unitless x1000"}

    # On a 30 m analysis grid, alignment reproduces the native composites.
    assert manifest["analysis_grid"]["resolution"] == 30.0
    for product in products:
        with rasterio.open(product.native_path) as native, rasterio.open(product.path) as aligned:
            assert native.res == (30.0, 30.0)
            assert np.allclose(native.read(1), aligned.read(1), atol=1e-6)

    # Both windows were searched with the AOI's own lon/lat bounds and the configured dates.
    expected_bbox = transform_bounds(AOI_CRS, "EPSG:4326", *AOI_BOUNDS)
    for bbox, _, _ in factory.calls:
        assert np.allclose(bbox, expected_bbox, atol=1e-5)
    assert [(start, end) for _, start, end in factory.calls] == [
        (date(2017, 6, 1), date(2017, 9, 30)),
        (date(2018, 6, 1), date(2018, 9, 30)),
    ]


def test_empty_window_fails_before_credentials_or_downloads(tmp_path: Path, monkeypatch) -> None:
    remote = _item("R1", datetime(2017, 7, 1, 18, 40, tzinfo=UTC), 5, href_root="https://example.invalid")
    monkeypatch.setattr(catalog, "search_hls_vi", lambda bbox, start, end: [remote] if start.year == 2017 else [])
    monkeypatch.setattr(catalog, "earthdata_gdal_options", lambda: pytest.fail("credentials checked before windows"))
    aoi = tmp_path / "aoi.shp"
    _write_aoi(aoi)
    with pytest.raises(EmptyWindowError, match="Window 'post'.*Widen the window"):
        build_hls_products(
            parse_remote_sensing_config(_block()), aoi, tmp_path / "out", grid_for_aoi(aoi, 30.0),
            continuous_resampling="bilinear",
        )


def test_window_emptied_by_cloud_max_names_the_cause(tmp_path: Path, monkeypatch) -> None:
    factory = _synthetic_scenes(tmp_path / "hls")
    monkeypatch.setattr(catalog, "search_hls_vi", factory.search)
    aoi = tmp_path / "aoi.shp"
    _write_aoi(aoi)
    with pytest.raises(EmptyWindowError, match=r"4 found, 4 above cloud_max=1"):
        build_hls_products(
            parse_remote_sensing_config(_block(cloud_max=1)), aoi, tmp_path / "out", grid_for_aoi(aoi, 30.0),
            continuous_resampling="bilinear",
        )


# ---------------------------------------------------------------- one grid across commands


def _shared_config(tmp_path: Path, target_res: float) -> tuple[dict, Path]:
    """One config, as a user would write it, read by the pipeline, soil and HLS commands."""
    pytest.importorskip("fiona")
    pytest.importorskip("landlab")
    data_dir, out_dir = tmp_path / "inputs", tmp_path / "outputs"
    data_dir.mkdir()
    out_dir.mkdir()
    # Source rasters sit on a 30 m grid offset from the analysis grid on purpose.
    transform = from_origin(AOI_BOUNDS[0] - 15.0, AOI_BOUNDS[3] + 15.0, 30.0, 30.0)
    shape = (7, 7)

    def tif(name: str, values: np.ndarray) -> Path:
        path = data_dir / f"{name}.tif"
        _write_raster(path, values.astype(np.float32), transform, AOI_CRS, nodata=-9999.0)
        return path

    dem = tif("dem", np.arange(49).reshape(shape) + 100.0)
    tif("burn", np.full(shape, 3.0))
    landcover = tif("landcover", np.full(shape, 42.0))
    soils = {
        "cec7_0_cm": 10.0, "anylithicdpt_cm": 200.0, "claytotal_0_cm": 25.0, "ph1to1h2o_0_cm": 650.0,
        "sandtotal_0_cm": 40.0, "silttotal_0_cm": 35.0, "dbovendry_0_cm": 140.0,
    }
    for key, value in soils.items():
        tif(key, np.full(shape, value))
    aoi = data_dir / "aoi.shp"
    _write_aoi(aoi)

    cfg = {
        "aoi": {"aoi": str(aoi)},
        "paths": {"output_dir": str(out_dir)},
        "raster": {"target_res": target_res, "resampling_method": "bilinear"},
        "fire": {"name": "Test Fire", "id": "TEST000"},
        "dem": {"source": "local", "path": str(dem)},
        "burn_severity": {
            "source": "local",
            "local": {"path": str(data_dir), "filename": "burn.tif", "resampling": "nearest"},
        },
        "dnbr": {"enabled": False},
        "feature_sources": {
            "rasters": {k: {"url": str(data_dir / f"{k}.tif"), "resampling": "bilinear"} for k in soils},
            "landcover": {"nlcd_local": {"url": str(landcover), "resampling": "nearest", "unzip": False}},
        },
        "remote_sensing": _block(),
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(cfg))
    return cfg, config_path


def test_every_command_lands_on_the_same_grid(tmp_path: Path, monkeypatch) -> None:
    from landslide_data_prep.analysis_grid import grid_from_config
    from landslide_data_prep.pipeline import run_raster_pipeline
    from landslide_data_prep.remote_sensing import cli_run
    from landslide_data_prep.soil_data.core import SoilVarSpec, harmonize_soil_layers

    factory = _synthetic_scenes(tmp_path / "hls")
    monkeypatch.setattr(catalog, "search_hls_vi", factory.search)
    cfg, config_path = _shared_config(tmp_path, target_res=10.0)
    grid = grid_from_config(cfg)
    assert (grid.width, grid.height, grid.resolution) == (18, 18, 10.0)
    out = tmp_path / "outputs"

    outputs = run_raster_pipeline(cfg, cleanup_intermediates=True)
    soil_manifest = harmonize_soil_layers(
        aoi_path=Path(cfg["aoi"]["aoi"]),
        output_dir=out / "soil",
        specs=[SoilVarSpec("cec7_0_cm", "cation__exchange_capacity", cfg["feature_sources"]["rasters"]["cec7_0_cm"]["url"], "bilinear")],
        grid=grid,
    )
    assert cli_run.main(["--config", str(config_path), "--max-workers", "2"]) == 0

    layers = (
        sorted(outputs.values())
        + sorted(str(p) for p in (out / "soil").glob("*.asc"))
        + sorted(str(p) for p in (out / "soil").glob("*.tif"))
        + sorted(str(p) for p in (out / "remote_sensing").glob("*.tif"))
    )
    assert len(layers) == len(outputs) + 2 + len(EXPECTED_KEYS)
    for layer in layers:
        check_on_grid(layer, grid)

    assert json.loads(Path(soil_manifest).read_text())["grid"] == grid.as_dict()
    assert not (out / "_aligned").exists() and not (out / "_downloads").exists()

    with rasterio.open(out / "remote_sensing" / "hls_ndvi_pre.tif") as src:
        ndvi = src.read(1)
    assert np.isclose(np.median(ndvi[ndvi != -9999.0]), 0.7, atol=1e-4)
    with rasterio.open(out / "remote_sensing" / "hls_clear_count_pre.tif") as src:
        counts = src.read(1)
    assert set(np.unique(counts[counts != -9999.0])) <= {2.0, 3.0}  # nearest keeps counts whole
