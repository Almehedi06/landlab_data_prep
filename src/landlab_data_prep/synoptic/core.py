"""Cache source JSON and assign interval station totals to the shared grid."""
import csv
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path

import fiona
import geopandas as gpd
import numpy as np
import rasterio
import requests
from shapely.geometry import Point, mapping
from shapely.ops import unary_union

from landlab_data_prep.analysis_grid import NODATA, aoi_mask, check_on_grid
from landlab_data_prep.preflight import load_and_validate_aoi
from landlab_data_prep.reproject_and_resample import convert_to_ascii

URL = "https://api.synopticdata.com/v2/stations/precip"


class SynopticError(RuntimeError):
    pass


def fetch(params, cache_dir, refresh=False):
    """Cache the response body as received; never persist or print the API token."""
    key = hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()
    path = Path(cache_dir) / "synoptic" / f"{key}.json"
    if path.exists() and not refresh:
        body = path.read_bytes()
    else:
        token = os.environ.get("SYNOPTIC_TOKEN")
        if not token:
            raise SynopticError("Set SYNOPTIC_TOKEN to download uncached precipitation requests.")
        try:
            response = requests.get(URL, params=params | {"token": token}, timeout=120)
            response.raise_for_status()
            body = response.content
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "unknown"
            raise SynopticError(
                f"Synoptic HTTP {status}; check token and account access at "
                "https://customer.synopticdata.com/."
            ) from None
        except requests.RequestException:
            raise SynopticError("Synoptic request failed; check connectivity and API access.") from None
    try:
        payload = json.loads(body)
        code = int(payload["SUMMARY"]["RESPONSE_CODE"])
    except (ValueError, KeyError, TypeError):
        raise SynopticError("Synoptic returned an invalid JSON response.") from None
    if code not in (1, 2):
        raise SynopticError(f"Synoptic API response code {code}; check token, access plan and request limits.")
    if code == 1 and payload.get("UNITS", {}).get("precipitation", "").lower() not in ("millimeters", "mm"):
        raise SynopticError("Synoptic response did not declare precipitation in millimeters.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return payload, path


def write_csv(path, rows, fields):
    with Path(path).open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def write_station_layer(path, stations, availability, grid, total_intervals):
    """A point layer in the analysis CRS, including stations with no usable data."""
    schema = {"geometry": "Point", "properties": {
        "station_index": "int", "stid": "str", "name": "str", "network": "str",
        "longitude": "float", "latitude": "float", "returned_records": "int",
        "valid_intervals": "int", "requested_intervals": "int", "availability_fraction": "float",
    }}
    with fiona.open(path, "w", driver="GPKG", layer="stations", schema=schema, crs=grid.crs) as dst:
        for stid, station in stations.items():
            returned, valid = availability[stid]
            point = gpd.GeoSeries([Point(station["longitude"], station["latitude"])], crs=4326)
            properties = dict(station, network=str(station["network"]), returned_records=returned,
                              valid_intervals=valid, requested_intervals=total_intervals,
                              availability_fraction=valid / total_intervals)
            dst.write({"geometry": mapping(point.to_crs(grid.crs).iloc[0]), "properties": properties})


def usable_total(record, start, end, hours):
    """Accept one complete service interval, retaining report timing separately."""
    try:
        value = float(record["total"])
        first = datetime.fromisoformat(record["first_report"].replace("Z", "+00:00"))
        last = datetime.fromisoformat(record["last_report"].replace("Z", "+00:00"))
        return (np.isfinite(value) and value >= 0 and float(record["interval"]) == hours
                and int(record["count"]) > 0 and start <= first <= last <= end)
    except (KeyError, ValueError, TypeError):
        return False


def nearest_grid(points, grid, mask, max_distance_km):
    """Nearest valid station per cell, bounded in metres; stable ties by station ID."""
    best = np.full(grid.shape, np.inf)
    values = np.full(grid.shape, NODATA, dtype="float32")
    ids = np.full(grid.shape, NODATA, dtype="float32")
    x = grid.transform.c + (np.arange(grid.width) + 0.5) * grid.resolution
    y = grid.transform.f - (np.arange(grid.height) + 0.5) * grid.resolution
    for number, px, py, total in points:
        distance = (x[None, :] - px)**2 + (y[:, None] - py)**2
        take = mask & (distance < best) & (distance <= (max_distance_km * 1000)**2)
        values[take], ids[take], best[take] = total, number, distance[take]
    distance = np.where(values != NODATA, np.sqrt(best) / 1000, NODATA).astype("float32")
    return values, ids, distance


def build_synoptic_forcing(config, aoi_path, output_dir, grid, cache_dir, refresh=False):
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    aoi = load_and_validate_aoi(aoi_path).to_crs(grid.crs)
    search = unary_union(aoi.geometry).buffer(config.buffer_km * 1000)
    bbox = gpd.GeoSeries([search], crs=grid.crs).to_crs(4326).total_bounds
    mask = aoi_mask(aoi_path, grid)
    if not mask.any():
        raise SynopticError("AOI contains no analysis-grid cell centres.")
    stations, observations, forcing, requests_log = {}, [], [], []
    availability = {}
    for start, end in config.windows:
        params = {"bbox": ",".join(map(str, bbox)), "start": start.strftime("%Y%m%d%H%M"),
                  "end": end.strftime("%Y%m%d%H%M"), "pmode": "intervals", "interval": config.interval_hours,
                  "interval_window": 0, "units": "precip|mm", "obtimezone": "UTC", "showemptystations": 1}
        payload, cached = fetch(params, cache_dir, refresh)
        requests_log.append({"parameters": params, "cache_file": str(cached.resolve()),
                             "sha256": hashlib.sha256(cached.read_bytes()).hexdigest()})
        points = []
        for station in sorted(payload.get("STATION", []), key=lambda s: s["STID"]):
            stid = station["STID"]
            lon, lat = float(station["LONGITUDE"]), float(station["LATITUDE"])
            point = gpd.GeoSeries([Point(lon, lat)], crs=4326).to_crs(grid.crs).iloc[0]
            if not search.covers(point):
                continue
            if stid not in stations:
                stations[stid] = {"station_index": len(stations) + 1, "stid": stid, "name": station.get("NAME", ""),
                                  "longitude": lon, "latitude": lat, "network": station.get("MNET_ID", "")}
            availability.setdefault(stid, [0, 0])
            records = station.get("OBSERVATIONS", {}).get("precipitation", [])
            valid = [r for r in records if usable_total(r, start, end, config.interval_hours)]
            availability[stid][0] += len(records)
            availability[stid][1] += int(len(valid) == 1)
            for r in records:
                observations.append({"stid": stid, "window_start": start.isoformat(), "window_end": end.isoformat(),
                                     "longitude": lon, "latitude": lat,
                                     **{k: r.get(k) for k in ("first_report", "last_report", "interval", "count",
                                                             "report_type", "total")},
                                     "used": len(valid) == 1 and r in valid})
            if len(valid) == 1:
                points.append((stations[stid]["station_index"], point.x, point.y, float(valid[0]["total"])))
        values, ids, distances = nearest_grid(points, grid, mask, config.max_distance_km)
        name = start.strftime("precip_%Y%m%dT%H%MZ")
        for folder, data, unit in (("aligned_tif/ppt", values, "mm/interval"),
                                   ("station_index", ids, "station_index"), ("station_distance_km", distances, "km")):
            path = out / folder / f"{name}.tif"
            path.parent.mkdir(parents=True, exist_ok=True)
            with rasterio.open(path, "w", **grid.profile()) as dst:
                dst.write(data, 1)
                dst.update_tags(source=URL, method="nearest_station", units=unit,
                                interval_start=start.isoformat(), interval_end=end.isoformat())
            check_on_grid(path, grid)
        tif = out / "aligned_tif/ppt" / f"{name}.tif"
        asc = Path(convert_to_ascii(str(tif), str(out / "asc/ppt"), grid=grid))
        covered = values != NODATA
        forcing.append({"datetime": start.isoformat(), "interval_end": end.isoformat(),
                        "precip_mm": float(values[covered].mean()) if covered.any() else "",
                        "coverage_fraction": float(covered.sum() / mask.sum()), "valid_stations": len(points),
                        "ppt_tif_path": tif.relative_to(out).as_posix(),
                        "ppt_asc_path": asc.relative_to(out).as_posix()})
    write_csv(out / "stations.csv", stations.values(),
              ["station_index", "stid", "name", "longitude", "latitude", "network"])
    write_station_layer(out / "stations.gpkg", stations, availability, grid, len(forcing))
    write_csv(out / "rainfall.csv", observations,
              ["stid", "window_start", "window_end", "longitude", "latitude", "first_report", "last_report",
               "interval", "count", "report_type", "total", "used"])
    result = out / "forcing_synoptic.csv"
    write_csv(result, forcing, list(forcing[0]))
    manifest = {"source": URL, "created_utc": datetime.now(timezone.utc).isoformat(),
                "product": "Synoptic derived interval precipitation; not FlowAlert intensities or alerts",
                "grid": grid.as_dict(), "aoi_path": str(aoi_path), "method": "nearest_station",
                "buffer_km": config.buffer_km, "max_distance_km": config.max_distance_km,
                "interval_hours": config.interval_hours, "requests": requests_log,
                "missing": "NoData; never filled with zero", "mean": "Mean of covered AOI cells only",
                "station_coordinates": "stations.csv gives first-seen location; rainfall.csv gives per-window location"}
    (out / "synoptic_manifest.json").write_text(json.dumps(manifest, indent=2))
    return result
