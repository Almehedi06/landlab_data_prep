"""Small, strict configuration for station rainfall forcing."""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import math



@dataclass(frozen=True)
class SynopticConfig:
    start: date
    end: date
    interval_hours: int
    buffer_km: float
    max_distance_km: float

    @property
    def windows(self):
        start = datetime.combine(self.start, time(), timezone.utc)
        stop = datetime.combine(self.end + timedelta(days=1), time(), timezone.utc)
        while start < stop:
            end = start + timedelta(hours=self.interval_hours)
            yield start, end
            start = end


class SynopticConfigError(ValueError):
    def __init__(self, problems):
        self.problems = problems
        super().__init__("Invalid synoptic config: " + "; ".join(problems))


def parse_synoptic_config(block):
    if not isinstance(block, dict):
        raise SynopticConfigError(["synoptic must be a mapping"])
    problems = []
    allowed = {"start", "end", "interval_hours", "buffer_km", "max_distance_km"}
    if set(block) - allowed:
        problems.append(f"synoptic: unknown keys {sorted(set(block) - allowed, key=str)}")
    dates = {}
    for key in ("start", "end"):
        try:
            dates[key] = date.fromisoformat(str(block.get(key, "")))
        except ValueError:
            problems.append(f"synoptic.{key}: expected YYYY-MM-DD")
    if len(dates) == 2:
        days = (dates["end"] - dates["start"]).days + 1
        if not 1 <= days <= 366:
            problems.append("synoptic: use an inclusive date range of 1 to 366 days")
        if dates["end"] >= datetime.now(timezone.utc).date():
            problems.append("synoptic.end must be before today (UTC)")
    hours = block.get("interval_hours", 1)
    if type(hours) is not int or hours not in (1, 2, 3, 4, 6, 8, 12, 24):
        problems.append("synoptic.interval_hours must be one of 1, 2, 3, 4, 6, 8, 12, 24")
    for key, default in (("buffer_km", 4), ("max_distance_km", 25)):
        value = block.get(key, default)
        if (type(value) not in (int, float) or not math.isfinite(value)
                or value < 0 or (key == "max_distance_km" and value == 0)):
            kind = "positive" if key == "max_distance_km" else "nonnegative"
            problems.append(f"synoptic.{key} must be finite and {kind}")
    if problems:
        raise SynopticConfigError(problems)
    return SynopticConfig(dates["start"], dates["end"], hours, block.get("buffer_km", 4),
                          block.get("max_distance_km", 25))
