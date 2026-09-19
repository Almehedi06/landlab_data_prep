"""Find HLS vegetation-index scenes and prepare Earthdata authentication.

The only module that knows catalog endpoints, collection names, or how NASA
Earthdata credentials are supplied. Everything else works on ``Scene`` objects.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import netrc
import os
from pathlib import Path
import threading
from typing import Callable, Iterable, Mapping

CMR_STAC_URL = "https://cmr.earthdata.nasa.gov/stac/LPCLOUD"
HLS_VI_COLLECTIONS = ("HLSL30_VI_2.0", "HLSS30_VI_2.0")
NATIVE_RESOLUTION_M = 30.0
FMASK_ASSET = "Fmask"
CLOUD_COVER_PROPERTY = "eo:cloud_cover"
EARTHDATA_HOST = "urs.earthdata.nasa.gov"
TOKEN_ENV_VAR = "EARTHDATA_TOKEN"

_BASE_GDAL_OPTIONS = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif",
    "GDAL_HTTP_MAX_RETRY": "5",
    "GDAL_HTTP_RETRY_DELAY": "2",
}


class EarthdataAuthError(RuntimeError):
    pass


@dataclass(frozen=True)
class Scene:
    """One HLS VI granule: the hrefs it needs and when it was observed."""

    id: str
    datetime: datetime  # timezone-aware UTC
    cloud_cover: float
    hrefs: Mapping[str, str]  # asset name -> href, always includes Fmask


def search_hls_vi(
    bbox_wgs84: tuple[float, float, float, float],
    start: date,
    end: date,
) -> list:
    """STAC items from both HLS VI collections within [start, end], inclusive UTC days."""
    # Imported here so the pipeline and offline tests do not need pystac-client
    # unless a search actually runs.
    from pystac_client import Client

    search = Client.open(CMR_STAC_URL).search(
        collections=list(HLS_VI_COLLECTIONS),
        bbox=list(bbox_wgs84),
        datetime=f"{start.isoformat()}T00:00:00Z/{end.isoformat()}T23:59:59Z",
        limit=100,
    )
    return list(search.items())


def scenes_from_items(
    items: Iterable,
    indices: Iterable[str],
    cloud_max: float,
) -> tuple[list[Scene], dict[str, int]]:
    """Drop scenes above ``cloud_max`` and fail loudly on unusable catalog items.

    Returns scenes sorted by (datetime, id) so every later step is deterministic.
    """
    needed = (FMASK_ASSET, *indices)
    problems: list[str] = []
    scenes: list[Scene] = []
    seen: set[str] = set()
    found = 0
    dropped_cloud = 0

    for item in items:
        found += 1
        if item.id in seen:  # pagination can repeat an item; never count it twice
            continue
        seen.add(item.id)

        cloud_cover = item.properties.get(CLOUD_COVER_PROPERTY)
        missing = [name for name in needed if name not in item.assets]
        if cloud_cover is None:
            problems.append(f"{item.id}: no '{CLOUD_COVER_PROPERTY}' property")
        if missing:
            problems.append(f"{item.id}: missing assets {missing}")
        if item.datetime is None:
            problems.append(f"{item.id}: no datetime")
        if cloud_cover is None or missing or item.datetime is None:
            continue

        if float(cloud_cover) > cloud_max:
            dropped_cloud += 1
            continue

        observed = item.datetime
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        scenes.append(
            Scene(
                id=item.id,
                datetime=observed.astimezone(timezone.utc),
                cloud_cover=float(cloud_cover),
                hrefs={name: item.assets[name].href for name in needed},
            )
        )

    if problems:
        raise ValueError("Unusable catalog items:\n" + "\n".join(f"  - {p}" for p in problems))

    scenes.sort(key=lambda s: (s.datetime, s.id))
    stats = {
        "items_found": found,
        "items_unique": len(seen),
        "dropped_above_cloud_max": dropped_cloud,
        "scenes_used": len(scenes),
    }
    return scenes, stats


def needs_earthdata_auth(scenes: Iterable[Scene]) -> bool:
    return any(href.startswith(("http://", "https://")) for s in scenes for href in s.hrefs.values())


def earthdata_gdal_options(
    environ: Mapping[str, str] | None = None,
    netrc_path: str | Path | None = None,
) -> dict[str, str]:
    """GDAL options that authenticate reads against NASA Earthdata.

    Two supported routes, checked in order: an ``EARTHDATA_TOKEN`` environment
    variable, then a netrc entry for urs.earthdata.nasa.gov. Raises before any
    download starts if neither is present.
    """
    environ = os.environ if environ is None else environ

    token = (environ.get(TOKEN_ENV_VAR) or "").strip()
    if token:
        return {**_BASE_GDAL_OPTIONS, "GDAL_HTTP_HEADERS": f"Authorization: Bearer {token}"}

    path = Path(netrc_path or environ.get("NETRC") or (Path.home() / ".netrc"))
    if path.exists():
        try:
            entry = netrc.netrc(str(path)).authenticators(EARTHDATA_HOST)
        except (netrc.NetrcParseError, OSError) as exc:
            raise EarthdataAuthError(f"Could not parse netrc file {path}: {exc}") from exc
        if entry:
            return {**_BASE_GDAL_OPTIONS, "GDAL_HTTP_NETRC": "YES", "GDAL_HTTP_NETRC_FILE": str(path)}

    raise EarthdataAuthError(
        "HLS downloads need NASA Earthdata credentials, and none were found. Either:\n"
        f"  - export {TOKEN_ENV_VAR}=<token from https://urs.earthdata.nasa.gov/profile>, or\n"
        f"  - add 'machine {EARTHDATA_HOST} login <user> password <pass>' to {path} "
        "(chmod 600)."
    )


def per_thread_options_factory(
    base_options: Mapping[str, str],
    cookie_dir: str | Path,
) -> Callable[[], dict[str, str]]:
    """Return a callable giving each reader thread its own GDAL options.

    The netrc route follows a login redirect that sets a session cookie. Threads
    sharing one cookie jar can corrupt it, so each thread gets its own file.
    """
    base = dict(base_options)
    if base.get("GDAL_HTTP_NETRC") != "YES":
        return lambda: dict(base)

    cookie_dir = Path(cookie_dir)

    def factory() -> dict[str, str]:
        jar = cookie_dir / f"earthdata_cookies_{threading.get_ident()}.txt"
        return {**base, "GDAL_HTTP_COOKIEFILE": str(jar), "GDAL_HTTP_COOKIEJAR": str(jar)}

    return factory
