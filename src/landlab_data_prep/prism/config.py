"""Strict parsing of the ``prism`` config block."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

VARIABLES = ("ppt", "tmin", "tmax")
RESOLUTIONS = ("800m", "4km")
# One download per day and variable; a mistyped year would mean thousands of downloads.
MAX_DAYS = 366
_KEYS = ("start", "end", "variables", "resolution")


class PrismConfigError(ValueError):
    def __init__(self, problems: list[str]) -> None:
        self.problems = list(problems)
        lines = "\n".join(f"  - {p}" for p in self.problems)
        super().__init__(f"Invalid prism config:\n{lines}")


@dataclass(frozen=True)
class PrismConfig:
    start: date
    end: date
    variables: tuple[str, ...]
    resolution: str

    @property
    def days(self) -> list[date]:
        return [self.start + timedelta(days=i) for i in range((self.end - self.start).days + 1)]


def parse_prism_config(block: object, today: date | None = None) -> PrismConfig:
    if not isinstance(block, dict):
        raise PrismConfigError([f"prism must be a mapping, got {type(block).__name__}"])
    today = today or date.today()
    problems: list[str] = []

    unknown = sorted(str(k) for k in block if k not in _KEYS)
    if unknown:
        problems.append(f"prism: unknown keys {unknown}; allowed keys are {sorted(_KEYS)}")
    missing = [k for k in _KEYS if k not in block]
    if missing:
        problems.append(f"prism: missing required keys {missing}")

    start = _parse_date(block["start"], "prism.start", problems) if "start" in block else None
    end = _parse_date(block["end"], "prism.end", problems) if "end" in block else None
    if start and end:
        if start > end:
            problems.append(f"prism: start {start} is after end {end}")
        elif end >= today:
            problems.append(f"prism.end: {end} is not in the past; PRISM daily grids exist up to yesterday")
        elif (end - start).days + 1 > MAX_DAYS:
            problems.append(f"prism: {(end - start).days + 1} days requested; split ranges longer than {MAX_DAYS} days")

    variables = block.get("variables")
    if "variables" in block:
        if not isinstance(variables, list) or not variables:
            problems.append(f"prism.variables must be a non-empty list drawn from {list(VARIABLES)}")
        else:
            bad = [v for v in variables if v not in VARIABLES]
            if bad:
                problems.append(f"prism.variables: unsupported {bad}; supported are {list(VARIABLES)}")
            dupes = sorted({v for v in variables if variables.count(v) > 1}, key=str)
            if dupes:
                problems.append(f"prism.variables: duplicated {dupes}")

    resolution = block.get("resolution")
    if "resolution" in block and resolution not in RESOLUTIONS:
        problems.append(f"prism.resolution={resolution!r} is not supported; choose one of {list(RESOLUTIONS)}")

    if problems:
        raise PrismConfigError(problems)
    return PrismConfig(start=start, end=end, variables=tuple(variables), resolution=resolution)


def _parse_date(value: object, where: str, problems: list[str]) -> date | None:
    # YAML turns an unquoted 2025-12-07 into a date; a quoted one stays a string.
    if isinstance(value, datetime):
        problems.append(f"{where}: expected a date like 2025-12-07, got a datetime {value}")
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            pass
    problems.append(f"{where}: expected a date like 2025-12-07, got {value!r}")
    return None
