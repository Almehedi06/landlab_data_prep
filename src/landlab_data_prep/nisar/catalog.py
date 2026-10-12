"""Find NISAR scenes over the AOI.

The only module that knows NISAR collection and granule names. Reading them
goes through landlab_data_prep.earthdata.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
import logging
import re

from landlab_data_prep.earthdata import CMR_GRANULES_URL, DATA_REL, cmr_granules

LOG = logging.getLogger(__name__)

# Collections per product, preferred first. Provisional products are calibrated and partially
# validated; Beta products are pre-calibration and cover the months before Provisional ones exist.
COLLECTIONS = {
    "GCOV": (("provisional", "NISAR_L2_GCOV_PROVISIONAL_V1"), ("beta", "NISAR_L2_GCOV_BETA_V1")),
    "SME2": (("provisional", "NISAR_L3_SME2_PROVISIONAL_V1"), ("beta", "NISAR_L3_SME2_BETA_V1")),
}
RELEASES = ("provisional", "beta")

_NAME = re.compile(
    r"NISAR_L[23]_(?P<processing>PR|UR|OD)_(?P<product>[A-Z0-9]{4})_(?P<cycle>\d{3})_(?P<track>\d{3})_"
    r"(?P<direction>[AD])_(?P<frame>\d{3})_(?P<mode>\d{4})_(?P<polarization>[A-Z]{4})_[AM]_"
    r"(?P<start>\d{8}T\d{6})_(?P<end>\d{8}T\d{6})_(?P<crid>[A-Z]\d{5})_[PMNF]_(?P<coverage>[FP])_[A-Z]_"
    r"(?P<counter>\d{3})"
)


class NisarError(RuntimeError):
    pass


@dataclass(frozen=True)
class Scene:
    """One NISAR granule, described by its name and the release it came from."""

    id: str
    url: str
    release: str  # provisional or beta
    start: datetime  # timezone-aware UTC
    track: str
    direction: str  # ascending or descending
    frame: str
    mode: str
    polarization: str
    frame_coverage: str  # full or partial
    crid: str
    counter: str

    @property
    def acquisition(self) -> tuple[str, str, str, datetime]:
        return (self.track, self.direction, self.frame, self.start)

    @property
    def stem(self) -> str:
        return f"{self.start:%Y%m%dT%H%M%S}_T{self.track}{self.direction[0].upper()}_F{self.frame}"


def parse_scene(name: str, url: str, release: str) -> Scene:
    match = _NAME.fullmatch(name)
    if not match:
        raise NisarError(f"Unrecognised NISAR granule name {name!r}")
    if release not in RELEASES:
        raise NisarError(f"Unknown NISAR release {release!r}")
    g = match.groupdict()
    return Scene(
        id=name,
        url=url,
        release=release,
        start=datetime.strptime(g["start"], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc),
        track=g["track"],
        direction="ascending" if g["direction"] == "A" else "descending",
        frame=g["frame"],
        mode=g["mode"],
        polarization=g["polarization"],
        frame_coverage="full" if g["coverage"] == "F" else "partial",
        crid=g["crid"],
        counter=g["counter"],
    )


def data_url(entry: Mapping) -> str:
    """The public HTTPS link to the granule's HDF5 file."""
    title = entry.get("title", "")
    urls = sorted({
        link["href"] for link in entry.get("links", [])
        if link.get("rel") == DATA_REL
        and link.get("href", "").startswith("https://")
        and link["href"].rsplit("/", 1)[-1] == f"{title}.h5"
        and "PRIVATE" not in link["href"]
    })
    if len(urls) != 1:
        raise NisarError(f"Expected one HTTPS download link for {title!r}, found {urls}")
    return urls[0]


def latest_processing(scenes: list[Scene]) -> tuple[list[Scene], list[str]]:
    """One scene per acquisition: the preferred release, then the latest processing.

    Also returns the ids of the scenes it set aside.
    """
    def rank(scene: Scene) -> tuple:
        return (-RELEASES.index(scene.release), scene.crid, scene.counter)

    best: dict[tuple, Scene] = {}
    for scene in scenes:
        kept = best.get(scene.acquisition)
        if kept is None or rank(scene) > rank(kept):
            best[scene.acquisition] = scene
    chosen = sorted(best.values(), key=lambda s: (s.start, s.id))
    superseded = sorted({s.id for s in scenes} - {s.id for s in chosen})
    return chosen, superseded


def search_scenes(
    product: str, bbox_lonlat: tuple[float, float, float, float], start: date, end: date
) -> tuple[list[Scene], dict]:
    """Scenes whose footprint meets the bounding box between start and end (inclusive UTC days)."""
    params = {
        "bounding_box": ",".join(f"{v:.6f}" for v in bbox_lonlat),
        "temporal": f"{start.isoformat()}T00:00:00Z,{end.isoformat()}T23:59:59Z",
        "sort_key": "start_date",
    }
    found: list[Scene] = []
    counts: dict[str, int] = {}
    for release, short_name in COLLECTIONS[product]:
        entries = cmr_granules({**params, "short_name": short_name})
        counts[short_name] = len(entries)
        found += [parse_scene(e["title"], data_url(e), release) for e in entries]
    scenes, superseded = latest_processing(found)
    return scenes, {"cmr_url": CMR_GRANULES_URL, "query": params, "granules_found": counts,
                    "superseded": superseded}
