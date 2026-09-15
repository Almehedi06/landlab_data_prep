"""Read HLS scenes onto one native grid and composite each window.

Scenes are read with nearest-neighbour onto the native 30 m grid through
analysis_grid.read_on_grid. For tiles in the grid's UTM zone that is an exact
pixel copy; for tiles from a neighbouring zone it is a reprojection that never
blends values. Putting products on the analysis grid happens afterwards, in
core.py, with analysis_grid.align_to_grid.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Callable, Mapping, Sequence
import warnings

import numpy as np
import rasterio

from landslide_data_prep.analysis_grid import Grid, read_on_grid
from landslide_data_prep.remote_sensing.catalog import FMASK_ASSET, Scene
from landslide_data_prep.remote_sensing.qa import FMASK_FILL, clear_mask

# HLS VI User Guide v2.0, product table: NDVI, NBR, NDMI are int16 with
# scale 0.0001, fill -19999, valid range -1 to 1.
VI_SCALE = 0.0001
VI_FILL = -19999
# If most valid pixels of a decoded scene fall outside +/-1.5, the scale factor
# is wrong for that file. Wider than the valid range so real values never trip it.
SCALE_SANITY_LIMIT = 1.5
# Per-window in-memory stack ceiling. Exceeding it raises before any download.
MAX_STACK_BYTES = 8 * 1024**3


@dataclass(frozen=True)
class WindowComposite:
    medians: Mapping[str, np.ndarray]  # index -> float32, NaN where no clear day
    clear_count: np.ndarray  # int32, clear solar days per pixel
    n_scenes: int
    days: tuple[date, ...]


def solar_day(observed_utc: datetime, longitude: float) -> date:
    """Local solar date. Tiles from one satellite pass share it; UTC dates may not."""
    return (observed_utc + timedelta(hours=longitude / 15.0)).date()


def group_by_solar_day(
    scenes: Sequence[Scene], longitude: float
) -> list[tuple[date, list[Scene]]]:
    groups: dict[date, list[Scene]] = {}
    for scene in sorted(scenes, key=lambda s: (s.datetime, s.id)):
        groups.setdefault(solar_day(scene.datetime, longitude), []).append(scene)
    return sorted(groups.items())


def decode_vi(raw: np.ndarray, fill: float, scale: float) -> np.ndarray:
    """int16 VI to float32 with NaN at fill."""
    out = raw.astype(np.float32) * np.float32(scale)
    out[raw == fill] = np.nan
    return out


def check_scale(values: np.ndarray, label: str) -> None:
    valid = values[np.isfinite(values)]
    if valid.size == 0:
        return
    outside = int(np.count_nonzero(np.abs(valid) > SCALE_SANITY_LIMIT))
    if outside > 0.5 * valid.size:
        raise ValueError(
            f"{label}: {outside} of {valid.size} valid pixels decode outside "
            f"+/-{SCALE_SANITY_LIMIT}. The scale factor is wrong for this file."
        )


def read_scene(
    scene: Scene,
    indices: Sequence[str],
    grid: Grid,
    gdal_options: Mapping[str, str],
) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    """Valid mask and decoded values on ``grid`` for one scene.

    A pixel is valid only if Fmask calls it clear AND every requested index has
    data, so all indices in a composite are built from the same observations.
    """
    with rasterio.Env(**gdal_options):
        fmask, fmask_fill, _ = read_on_grid(
            scene.hrefs[FMASK_ASSET], grid, "nearest", default_src_nodata=FMASK_FILL
        )
        valid = clear_mask(fmask, fill=fmask_fill)
        values: dict[str, np.ndarray] = {}
        for name in indices:
            raw, fill, declared_scale = read_on_grid(
                scene.hrefs[name], grid, "nearest", default_src_nodata=VI_FILL
            )
            # rasterio never applies scale on read. Use the file's declared scale
            # when it has one, otherwise the documented one; check_scale catches
            # either being wrong.
            scale = declared_scale if declared_scale not in (None, 1.0) else VI_SCALE
            decoded = decode_vi(raw, fill=fill, scale=scale)
            check_scale(decoded, f"{scene.id} {name}")
            valid &= np.isfinite(decoded)
            values[name] = decoded

    for decoded in values.values():
        decoded[~valid] = np.nan
    return valid, values


def composite_window(
    scenes: Sequence[Scene],
    indices: Sequence[str],
    grid: Grid,
    longitude: float,
    options_factory: Callable[[], Mapping[str, str]],
    max_workers: int = 8,
) -> WindowComposite:
    """Median over clear solar days; scenes sharing a solar day are averaged first.

    Averaging within a day stops overlapping tiles of one pass being counted as
    independent observations.
    """
    if not scenes:
        raise ValueError("composite_window needs at least one scene")

    groups = group_by_solar_day(scenes, longitude)
    n_days = len(groups)
    needed = n_days * grid.width * grid.height * (4 * len(indices) + 1)
    if needed > MAX_STACK_BYTES:
        raise MemoryError(
            f"Compositing {n_days} days x {len(indices)} indices on a "
            f"{grid.width}x{grid.height} grid needs {needed / 1024**3:.1f} GiB, over the "
            f"{MAX_STACK_BYTES / 1024**3:.0f} GiB limit. Shorten the windows or split the AOI."
        )

    ordered = [scene for _, members in groups for scene in members]
    day_of = [d for d, (_, members) in enumerate(groups) for _ in members]

    sums = {name: np.zeros((n_days, *grid.shape), dtype=np.float32) for name in indices}
    obs = np.zeros((n_days, *grid.shape), dtype=np.uint8)

    def work(scene: Scene):
        return read_scene(scene, indices, grid, options_factory())

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for d, (valid, values) in zip(day_of, pool.map(work, ordered)):
            obs[d] += valid
            for name in indices:
                sums[name][d] += np.where(valid, values[name], np.float32(0.0))

    no_obs = obs == 0
    with np.errstate(invalid="ignore", divide="ignore"):
        for name in indices:
            sums[name] /= obs  # in place: sums become per-day means
            sums[name][no_obs] = np.nan  # explicit, not relying on 0/0

    clear_count = np.count_nonzero(~no_obs, axis=0).astype(np.int32)
    medians = {}
    for name in indices:
        median = _nanmedian_axis0(sums[name])
        median[clear_count == 0] = np.nan
        medians[name] = median

    return WindowComposite(
        medians=medians,
        clear_count=clear_count,
        n_scenes=len(ordered),
        days=tuple(day for day, _ in groups),
    )


def _nanmedian_axis0(stack: np.ndarray, chunk_rows: int = 256) -> np.ndarray:
    """nanmedian over time, row-chunked to cap the temporary copy numpy makes."""
    out = np.full(stack.shape[1:], np.nan, dtype=np.float32)
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="All-NaN slice encountered", category=RuntimeWarning)
        for r0 in range(0, stack.shape[1], chunk_rows):
            out[r0 : r0 + chunk_rows] = np.nanmedian(stack[:, r0 : r0 + chunk_rows, :], axis=0)
    return out
