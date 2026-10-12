"""Date parsing shared by the config blocks."""

from __future__ import annotations

from datetime import date, datetime


def parse_date(value: object, where: str, problems: list[str]) -> date | None:
    """A config date, or None with the problem added to ``problems``.

    YAML turns an unquoted 2025-12-07 into a date; a quoted one stays a string.
    """
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
