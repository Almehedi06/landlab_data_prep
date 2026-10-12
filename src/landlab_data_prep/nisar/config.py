"""Strict parsing of the ``nisar`` config block."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from landlab_data_prep.dates import parse_date

# The layers each NISAR product offers, by the names the config uses.
PRODUCT_LAYERS = {
    # Radar backscatter: the real-valued terms of the covariance matrix, as the scenes' mode acquired them.
    "GCOV": ("HHHH", "HVHV", "VVVV", "VHVH", "RHRH", "RVRV"),
    # Soil moisture: NISAR's combined estimate, its three algorithms, and whether NISAR recommends the retrieval.
    "SME2": ("combined", "DSG", "PMI", "TSR", "recommended"),
}
# NISAR scenes start in October 2025; a mistyped year would mean hundreds of scenes.
MAX_DAYS = 366
_KEYS = ("start", "end", "layers")


class NisarConfigError(ValueError):
    def __init__(self, problems: list[str]) -> None:
        self.problems = list(problems)
        lines = "\n".join(f"  - {p}" for p in self.problems)
        super().__init__(f"Invalid nisar config:\n{lines}")


@dataclass(frozen=True)
class NisarConfig:
    start: date
    end: date
    layers: dict[str, tuple[str, ...]]  # product -> layers, in PRODUCT_LAYERS order of products


def parse_nisar_config(block: object, today: date | None = None) -> NisarConfig:
    if not isinstance(block, dict):
        raise NisarConfigError([f"nisar must be a mapping, got {type(block).__name__}"])
    today = today or date.today()
    problems: list[str] = []

    unknown = sorted(str(k) for k in block if k not in _KEYS)
    if unknown:
        problems.append(f"nisar: unknown keys {unknown}; allowed keys are {sorted(_KEYS)}")
    missing = [k for k in _KEYS if k not in block]
    if missing:
        problems.append(f"nisar: missing required keys {missing}")

    start = parse_date(block["start"], "nisar.start", problems) if "start" in block else None
    end = parse_date(block["end"], "nisar.end", problems) if "end" in block else None
    if start and end:
        if start > end:
            problems.append(f"nisar: start {start} is after end {end}")
        elif end > today:
            problems.append(f"nisar.end: {end} is in the future")
        elif (end - start).days + 1 > MAX_DAYS:
            problems.append(f"nisar: {(end - start).days + 1} days requested; split ranges longer than {MAX_DAYS} days")

    layers = block.get("layers")
    chosen: dict[str, tuple[str, ...]] = {}
    if "layers" in block:
        if not isinstance(layers, dict) or not layers:
            problems.append(f"nisar.layers must map one or more of {list(PRODUCT_LAYERS)} to a list of layers")
        else:
            for product in layers:
                if product not in PRODUCT_LAYERS:
                    problems.append(f"nisar.layers: unknown product {product!r}; choose from {list(PRODUCT_LAYERS)}")
            for product, offered in PRODUCT_LAYERS.items():
                if product not in layers:
                    continue
                names = layers[product]
                where = f"nisar.layers.{product}"
                if not isinstance(names, list) or not names:
                    problems.append(f"{where} must be a non-empty list drawn from {list(offered)}")
                    continue
                bad = [v for v in names if v not in offered]
                if bad:
                    problems.append(f"{where}: unsupported {bad}; supported are {list(offered)}")
                dupes = sorted({v for v in names if names.count(v) > 1}, key=str)
                if dupes:
                    problems.append(f"{where}: duplicated {dupes}")
                chosen[product] = tuple(names)

    if problems:
        raise NisarConfigError(problems)
    return NisarConfig(start=start, end=end, layers=chosen)
