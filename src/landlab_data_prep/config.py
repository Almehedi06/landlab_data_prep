"""Strict validation of the main YAML config.

Every problem is collected and reported together. Unknown keys are errors at
every level, because a silently ignored typo gives a plausible run with the
wrong settings. Each command requires only the sections it uses, and any other
section that is present is still checked in full, so one config stays valid for
every command.
"""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any

import yaml

from landlab_data_prep.analysis_grid import RESAMPLING
from landlab_data_prep.remote_sensing.config import RemoteSensingConfigError, parse_remote_sensing_config
from landlab_data_prep.soil_data.core import DEFAULT_SOIL_SPECS

SOIL_KEYS = tuple(DEFAULT_SOIL_SPECS)
SOURCE_KINDS = ("local", "remote", "remote_then_local", "auto")
DOWNLOADING_KINDS = ("remote", "remote_then_local", "auto")
DEM_SOURCES = ("local", "bmi-topography")
SECTIONS = ("aoi", "paths", "fire", "raster", "dem", "burn_severity", "dnbr", "feature_sources", "remote_sensing")
REQUIRED_SECTIONS = {
    "pipeline": ("aoi", "paths", "raster", "dem", "burn_severity", "dnbr", "feature_sources"),
    "soil": ("aoi", "paths", "raster", "feature_sources"),
    "remote_sensing": ("aoi", "paths", "raster", "remote_sensing"),
    "dem_difference": ("aoi", "raster"),
    "export": ("aoi", "raster"),
}
RETIRED_KEYS = {
    "inputs": "remove it; it was never read",
    "target": "remove it; it was never read",
    "paths.input_dir": "remove it; it was never read",
    "fire.state": "remove it; it was never read",
    "fire.year": "remove it; it was never read",
    "dem.cache_dir": "use paths.cache_dir",
    "dem.output_format": "remove it; DEMs are always downloaded as GeoTIFF",
}


class ConfigError(ValueError):
    def __init__(self, source: str, problems: list[str]) -> None:
        self.problems = list(problems)
        lines = "\n".join(f"  - {p}" for p in self.problems)
        super().__init__(f"Invalid config {source}:\n{lines}")


def load_config(path: str | Path, command: str) -> dict:
    """Read a YAML config and validate it for ``command``."""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"Config not found: {path}")
    cfg = yaml.safe_load(path.read_text())
    validate_config(cfg, command, source=str(path))
    return cfg


def validate_config(cfg: Any, command: str, source: str = "config") -> None:
    if command not in REQUIRED_SECTIONS:
        raise ValueError(f"Unknown command {command!r}; expected one of {list(REQUIRED_SECTIONS)}")
    if not isinstance(cfg, dict):
        raise ConfigError(source, ["the file must contain a mapping of sections"])

    p: list[str] = []
    required = REQUIRED_SECTIONS[command]
    _check_keys(cfg, "", required, [s for s in SECTIONS if s not in required], p)
    present = {
        name for name in SECTIONS
        if name in cfg and name != "remote_sensing" and _is_mapping(cfg[name], name, p)
    }

    if "aoi" in present:
        _check_keys(cfg["aoi"], "aoi", ("aoi",), (), p)
        _text(cfg["aoi"], "aoi", "aoi", p)
    if "paths" in present:
        _check_keys(cfg["paths"], "paths", ("output_dir",), ("cache_dir",), p)
        _text(cfg["paths"], "paths", "output_dir", p)
        _text(cfg["paths"], "paths", "cache_dir", p)
    if "fire" in present:
        fire = cfg["fire"]
        _check_keys(fire, "fire", (), ("name", "id", "post_image_date"), p)
        _text(fire, "fire", "name", p)
        _text(fire, "fire", "id", p)
        date = fire.get("post_image_date", "")
        if date not in ("", None) and not re.fullmatch(r"\d{8}", str(date)):
            p.append(f"fire.post_image_date: expected YYYYMMDD or an empty string, got {date!r}")
    if "raster" in present:
        _check_keys(cfg["raster"], "raster", ("target_res", "resampling_method"), (), p)
        _number(cfg["raster"], "raster", "target_res", p, strictly_positive=True)
        _choice(cfg["raster"], "raster", "resampling_method", RESAMPLING, p)
    if "dem" in present:
        _check_dem(cfg["dem"], p)
    if "burn_severity" in present:
        _check_source_block(cfg["burn_severity"], "burn_severity", False, p)
    if "dnbr" in present:
        _check_source_block(cfg["dnbr"], "dnbr", True, p)
    if "feature_sources" in present:
        _check_feature_sources(cfg["feature_sources"], command, p)
    if "remote_sensing" in cfg:
        try:
            parse_remote_sensing_config(cfg["remote_sensing"])
        except RemoteSensingConfigError as exc:
            p.extend(exc.problems)
    if command in ("pipeline", "soil"):
        _check_downloads(cfg, command, p)

    if p:
        raise ConfigError(source, p)


def _check_dem(dem: dict, p: list[str]) -> None:
    _check_keys(dem, "dem", ("source",), ("path", "dem_type", "buffer_deg", "api_key"), p)
    _choice(dem, "dem", "source", DEM_SOURCES, p)
    if dem.get("source") == "local":
        _require(dem, "dem", ("path",), "source is local", p)
        _text(dem, "dem", "path", p)
    elif dem.get("source") == "bmi-topography":
        _require(dem, "dem", ("dem_type", "buffer_deg"), "source is bmi-topography", p)
        _text(dem, "dem", "dem_type", p)
        _number(dem, "dem", "buffer_deg", p, strictly_positive=False)
    if dem.get("api_key") is not None and not isinstance(dem["api_key"], str):
        p.append(f"dem.api_key: expected a string, got {type(dem['api_key']).__name__}")


def _check_source_block(block: dict, name: str, can_disable: bool, p: list[str]) -> None:
    if can_disable:
        _check_keys(block, name, ("enabled",), ("source", "local", "remote"), p)
        _flag(block, name, "enabled", p)
        active = block.get("enabled") is True
        if active and "source" not in block:
            p.append(f"{name}.source: missing")
    else:
        _check_keys(block, name, ("source",), ("local", "remote"), p)
        active = True
    _choice(block, name, "source", SOURCE_KINDS, p)

    where = f"{name}.local"
    if "local" in block and _is_mapping(block["local"], where, p):
        _check_keys(block["local"], where, ("path", "filename", "resampling"), (), p)
        _text(block["local"], where, "path", p)
        _text(block["local"], where, "filename", p)
        _choice(block["local"], where, "resampling", RESAMPLING, p)
    where = f"{name}.remote"
    if "remote" in block and _is_mapping(block["remote"], where, p):
        remote = block["remote"]
        _check_keys(remote, where, ("base_url", "candidates", "resampling"), (), p)
        _text(remote, where, "base_url", p)
        candidates = remote.get("candidates")
        if "candidates" in remote and (
            not isinstance(candidates, list) or not candidates or not all(isinstance(c, str) and c for c in candidates)
        ):
            p.append(f"{where}.candidates: expected a non-empty list of URL patterns")
        _choice(remote, where, "resampling", RESAMPLING, p)

    source = block.get("source")
    if active and source in SOURCE_KINDS:
        if source != "remote" and "local" not in block:
            p.append(f"{name}.local: missing; source {source} reads a local file")
        if source != "local" and "remote" not in block:
            p.append(f"{name}.remote: missing; source {source} downloads")


def _check_feature_sources(fs: dict, command: str, p: list[str]) -> None:
    needs_landcover = command == "pipeline"
    _check_keys(
        fs,
        "feature_sources",
        ("rasters", "landcover") if needs_landcover else ("rasters",),
        () if needs_landcover else ("landcover",),
        p,
    )
    rasters = fs.get("rasters")
    if rasters is not None and _is_mapping(rasters, "feature_sources.rasters", p):
        for key, info in rasters.items():
            where = f"feature_sources.rasters.{key}"
            if _is_mapping(info, where, p):
                _check_keys(info, where, ("url", "resampling"), (), p)
                _text(info, where, "url", p)
                _choice(info, where, "resampling", RESAMPLING, p)
        if command in ("pipeline", "soil"):
            missing = [k for k in SOIL_KEYS if k not in rasters]
            if missing:
                p.append(f"feature_sources.rasters: missing soil layers {missing}")
    landcover = fs.get("landcover")
    if landcover is not None and _is_mapping(landcover, "feature_sources.landcover", p):
        if len(landcover) != 1:
            p.append(f"feature_sources.landcover: expected exactly one entry, got {len(landcover)}")
        for key, info in landcover.items():
            where = f"feature_sources.landcover.{key}"
            if _is_mapping(info, where, p):
                _check_keys(info, where, ("url", "resampling", "unzip"), (), p)
                _text(info, where, "url", p)
                _choice(info, where, "resampling", RESAMPLING, p)
                _flag(info, where, "unzip", p)


def _check_downloads(cfg: dict, command: str, p: list[str]) -> None:
    """Requirements that depend on which sources download rather than read local files."""

    def section(name: str) -> dict:
        value = cfg.get(name)
        return value if isinstance(value, dict) else {}

    fs = section("feature_sources")
    downloads: list[str] = []
    groups = ("rasters", "landcover") if command == "pipeline" else ("rasters",)
    for group in groups:
        entries = fs.get(group) if isinstance(fs.get(group), dict) else {}
        for key, info in entries.items():
            if isinstance(info, dict) and _is_url(info.get("url")):
                downloads.append(f"feature_sources.{group}.{key}")

    fire_needed_by: list[str] = []
    if command == "pipeline":
        if section("dem").get("source") == "bmi-topography":
            downloads.append("dem")
        if section("burn_severity").get("source") in DOWNLOADING_KINDS:
            downloads.append("burn_severity")
            fire_needed_by.append("burn_severity")
        dnbr = section("dnbr")
        if dnbr.get("enabled") is True and dnbr.get("source") in DOWNLOADING_KINDS:
            downloads.append("dnbr")
            fire_needed_by.append("dnbr")
            if not str(section("fire").get("post_image_date") or "").strip():
                p.append("fire.post_image_date: required because dNBR is downloaded")

    if downloads and not section("paths").get("cache_dir"):
        p.append(f"paths.cache_dir: required because these sources download: {downloads}")
    for key in ("name", "id"):
        if fire_needed_by and not section("fire").get(key):
            p.append(f"fire.{key}: required because {' and '.join(fire_needed_by)} download from BAER")


def _check_keys(mapping: dict, where: str, required, optional, p: list[str]) -> None:
    allowed = set(required) | set(optional)
    prefix = f"{where}." if where else ""
    for key in mapping:
        if key not in allowed:
            dotted = f"{prefix}{key}"
            hint = RETIRED_KEYS.get(dotted) or f"allowed keys are {sorted(allowed)}"
            p.append(f"{dotted}: unknown key; {hint}")
    for key in required:
        if key not in mapping:
            p.append(f"{prefix}{key}: missing")


def _require(mapping: dict, where: str, keys, reason: str, p: list[str]) -> None:
    for key in keys:
        if key not in mapping:
            p.append(f"{where}.{key}: missing; required when {reason}")


def _is_mapping(value: Any, where: str, p: list[str]) -> bool:
    if isinstance(value, dict):
        return True
    p.append(f"{where}: expected a mapping, got {type(value).__name__}")
    return False


def _text(mapping: dict, where: str, key: str, p: list[str]) -> None:
    if key in mapping and (not isinstance(mapping[key], str) or not mapping[key].strip()):
        p.append(f"{where}.{key}: expected a non-empty string, got {mapping[key]!r}")


def _number(mapping: dict, where: str, key: str, p: list[str], *, strictly_positive: bool) -> None:
    if key not in mapping:
        return
    value = mapping[key]
    ok = isinstance(value, (int, float)) and not isinstance(value, bool)
    ok = ok and (value > 0 if strictly_positive else value >= 0)
    if not ok:
        kind = "a positive number" if strictly_positive else "a number of zero or more"
        p.append(f"{where}.{key}: expected {kind}, got {value!r}")


def _choice(mapping: dict, where: str, key: str, choices, p: list[str]) -> None:
    if key in mapping and mapping[key] not in choices:
        p.append(f"{where}.{key}: expected one of {list(choices)}, got {mapping[key]!r}")


def _flag(mapping: dict, where: str, key: str, p: list[str]) -> None:
    if key in mapping and not isinstance(mapping[key], bool):
        p.append(f"{where}.{key}: expected true or false, got {mapping[key]!r}")


def _is_url(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(("http://", "https://"))
