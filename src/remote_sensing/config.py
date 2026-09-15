"""Strict parsing of the ``remote_sensing`` config block.

Every problem is collected and raised together, so one run shows the full list.
Unknown keys are errors: a silently ignored typo in a window date produces a
plausible composite from the wrong season, which nothing downstream can detect.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

SUPPORTED_INDICES = ("NBR", "NDVI", "NDMI")
SUPPORTED_COMPOSITING = ("median",)
SUPPORTED_RESAMPLING = ("nearest", "bilinear", "cubic", "average")
WINDOW_NAMES = ("pre", "post")

_TOP_REQUIRED = ("windows", "indices", "cloud_max", "compositing")
_TOP_OPTIONAL = ("resampling",)
_WINDOW_KEYS = ("start", "end")


class RemoteSensingConfigError(ValueError):
    def __init__(self, problems: list[str]) -> None:
        self.problems = list(problems)
        lines = "\n".join(f"  - {p}" for p in self.problems)
        super().__init__(f"Invalid remote_sensing config:\n{lines}")


@dataclass(frozen=True)
class Window:
    name: str
    start: date
    end: date


@dataclass(frozen=True)
class RemoteSensingConfig:
    pre: Window
    post: Window
    indices: tuple[str, ...]
    cloud_max: float
    compositing: str
    # None means "use the repo-wide raster.resampling_method".
    resampling: str | None

    @property
    def windows(self) -> tuple[Window, Window]:
        return (self.pre, self.post)


def parse_remote_sensing_config(block: object) -> RemoteSensingConfig:
    if not isinstance(block, dict):
        raise RemoteSensingConfigError(
            [f"remote_sensing must be a mapping, got {type(block).__name__}"]
        )

    problems: list[str] = []
    _check_keys(block, _TOP_REQUIRED, _TOP_OPTIONAL, "remote_sensing", problems)

    windows = _parse_windows(block.get("windows"), problems) if "windows" in block else {}
    indices = _parse_indices(block["indices"], problems) if "indices" in block else ()
    cloud_max = _parse_cloud_max(block["cloud_max"], problems) if "cloud_max" in block else None

    compositing = block.get("compositing")
    if "compositing" in block and compositing not in SUPPORTED_COMPOSITING:
        problems.append(
            f"remote_sensing.compositing={compositing!r} is not supported; "
            f"choose one of {list(SUPPORTED_COMPOSITING)}"
        )

    resampling = block.get("resampling")
    if resampling is not None and resampling not in SUPPORTED_RESAMPLING:
        problems.append(
            f"remote_sensing.resampling={resampling!r} is not supported; "
            f"choose one of {list(SUPPORTED_RESAMPLING)}"
        )

    if problems:
        raise RemoteSensingConfigError(problems)

    return RemoteSensingConfig(
        pre=windows["pre"],
        post=windows["post"],
        indices=indices,
        cloud_max=cloud_max,
        compositing=compositing,
        resampling=resampling,
    )


def _check_keys(
    mapping: dict,
    required: tuple[str, ...],
    optional: tuple[str, ...],
    where: str,
    problems: list[str],
) -> None:
    allowed = set(required) | set(optional)
    unknown = sorted(str(k) for k in mapping if k not in allowed)
    if unknown:
        problems.append(f"{where}: unknown keys {unknown}; allowed keys are {sorted(allowed)}")
    missing = [k for k in required if k not in mapping]
    if missing:
        problems.append(f"{where}: missing required keys {missing}")


def _parse_windows(raw: object, problems: list[str]) -> dict[str, Window]:
    where = "remote_sensing.windows"
    if not isinstance(raw, dict):
        problems.append(f"{where} must be a mapping with keys {list(WINDOW_NAMES)}")
        return {}
    _check_keys(raw, WINDOW_NAMES, (), where, problems)

    windows: dict[str, Window] = {}
    for name in WINDOW_NAMES:
        if name not in raw:
            continue
        w_where = f"{where}.{name}"
        w = raw[name]
        if not isinstance(w, dict):
            problems.append(f"{w_where} must be a mapping with keys {list(_WINDOW_KEYS)}")
            continue
        _check_keys(w, _WINDOW_KEYS, (), w_where, problems)
        start = _parse_date(w["start"], f"{w_where}.start", problems) if "start" in w else None
        end = _parse_date(w["end"], f"{w_where}.end", problems) if "end" in w else None
        if start is None or end is None:
            continue
        if start > end:
            problems.append(f"{w_where}: start {start} is after end {end}")
            continue
        windows[name] = Window(name=name, start=start, end=end)

    if "pre" in windows and "post" in windows and windows["pre"].end >= windows["post"].start:
        problems.append(
            f"{where}: pre must end before post starts "
            f"(pre.end={windows['pre'].end}, post.start={windows['post'].start})"
        )
    return windows


def _parse_date(value: object, where: str, problems: list[str]) -> date | None:
    # YAML turns an unquoted 2017-06-01 into a date; a quoted one stays a string.
    if isinstance(value, datetime):
        problems.append(f"{where}: expected a date like 2017-06-01, got a datetime {value}")
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            pass
    problems.append(f"{where}: expected a date like 2017-06-01, got {value!r}")
    return None


def _parse_indices(raw: object, problems: list[str]) -> tuple[str, ...]:
    where = "remote_sensing.indices"
    if not isinstance(raw, list) or not raw:
        problems.append(f"{where} must be a non-empty list, e.g. {list(SUPPORTED_INDICES)}")
        return ()
    bad = [v for v in raw if v not in SUPPORTED_INDICES]
    if bad:
        problems.append(
            f"{where}: unsupported {bad}; supported are {list(SUPPORTED_INDICES)} "
            "(names are case-sensitive)"
        )
    dupes = sorted({v for v in raw if raw.count(v) > 1}, key=str)
    if dupes:
        problems.append(f"{where}: duplicated {dupes}")
    return tuple(raw)


def _parse_cloud_max(raw: object, problems: list[str]) -> float | None:
    where = "remote_sensing.cloud_max"
    # bool is an int subclass; `cloud_max: yes` must not become 1.
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        problems.append(f"{where} must be a number from 0 to 100, got {raw!r}")
        return None
    if not 0 <= raw <= 100:
        problems.append(f"{where} must be between 0 and 100, got {raw}")
        return None
    return float(raw)
