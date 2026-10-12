"""Strict parsing of the ``smap`` config block."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from landlab_data_prep.dates import parse_date

# SMAP L4 soil moisture layers: 0-5 cm, 0-100 cm, and the whole profile to bedrock.
LAYERS = ("sm_surface", "sm_rootzone", "sm_profile")
FIRST_DAY = date(2015, 3, 31)
# Eight files a day; a mistyped year would mean thousands of reads.
MAX_DAYS = 366
_KEYS = ("start", "end", "layers")


class SmapConfigError(ValueError):
    def __init__(self, problems: list[str]) -> None:
        self.problems = list(problems)
        lines = "\n".join(f"  - {p}" for p in self.problems)
        super().__init__(f"Invalid smap config:\n{lines}")


@dataclass(frozen=True)
class SmapConfig:
    start: date
    end: date
    layers: tuple[str, ...]


def parse_smap_config(block: object, today: date | None = None) -> SmapConfig:
    if not isinstance(block, dict):
        raise SmapConfigError([f"smap must be a mapping, got {type(block).__name__}"])
    today = today or date.today()
    problems: list[str] = []

    unknown = sorted(str(k) for k in block if k not in _KEYS)
    if unknown:
        problems.append(f"smap: unknown keys {unknown}; allowed keys are {sorted(_KEYS)}")
    missing = [k for k in _KEYS if k not in block]
    if missing:
        problems.append(f"smap: missing required keys {missing}")

    start = parse_date(block["start"], "smap.start", problems) if "start" in block else None
    end = parse_date(block["end"], "smap.end", problems) if "end" in block else None
    if start and end:
        if start > end:
            problems.append(f"smap: start {start} is after end {end}")
        elif start < FIRST_DAY:
            problems.append(f"smap.start: {start} is before SMAP L4 begins on {FIRST_DAY}")
        elif end > today:
            problems.append(f"smap.end: {end} is in the future")
        elif (end - start).days + 1 > MAX_DAYS:
            problems.append(f"smap: {(end - start).days + 1} days requested; split ranges longer than {MAX_DAYS} days")

    layers = block.get("layers")
    if "layers" in block:
        if not isinstance(layers, list) or not layers:
            problems.append(f"smap.layers must be a non-empty list drawn from {list(LAYERS)}")
        else:
            bad = [v for v in layers if v not in LAYERS]
            if bad:
                problems.append(f"smap.layers: unsupported {bad}; supported are {list(LAYERS)}")
            dupes = sorted({v for v in layers if layers.count(v) > 1}, key=str)
            if dupes:
                problems.append(f"smap.layers: duplicated {dupes}")

    if problems:
        raise SmapConfigError(problems)
    return SmapConfig(start=start, end=end, layers=tuple(layers))
