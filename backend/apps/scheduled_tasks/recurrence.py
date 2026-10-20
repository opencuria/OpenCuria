"""Pure recurring schedule calculations with explicit DST policy."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MAX_CRON_EXPRESSIONS = 32
MAX_CRON_LENGTH = 1000
# Covers Feb 29 schedules across a skipped leap year (e.g. 2100).
_CRON_SEARCH_DAYS = 366 * 9

_MONTH_NAMES = {
    name: index
    for index, name in enumerate(
        "JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split(), start=1
    )
}
_DAY_NAMES = {
    name: index
    for index, name in enumerate(["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"])
}
_MACROS = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}


def valid_timezone(value: str) -> ZoneInfo:
    """Resolve an IANA zone or raise a user-facing validation error."""
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, TypeError) as exc:
        raise ValueError(f"Unknown IANA timezone: {value}") from exc


@dataclass(frozen=True)
class CronSpec:
    """One parsed five-field cron expression (Vixie semantics)."""

    minutes: tuple[int, ...]
    hours: tuple[int, ...]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]  # 0 = Sunday … 6 = Saturday
    day_restricted: bool
    weekday_restricted: bool

    def matches_date(self, day: date) -> bool:
        if day.month not in self.months:
            return False
        dom = day.day in self.days
        dow = (day.weekday() + 1) % 7 in self.weekdays
        # Classic cron: when both day fields are restricted either may match.
        if self.day_restricted and self.weekday_restricted:
            return dom or dow
        return dom and dow


def _parse_value(raw: str, names: dict[str, int], label: str) -> int:
    upper = raw.upper()
    if upper in names:
        return names[upper]
    if not raw.isdigit():
        raise ValueError(f"Invalid {label} value '{raw}' in cron expression")
    return int(raw)


def _parse_field(
    raw: str, low: int, high: int, label: str, names: dict[str, int] | None = None
) -> set[int]:
    names = names or {}
    values: set[int] = set()
    for part in raw.split(","):
        if not part:
            raise ValueError(f"Empty list item in cron {label} field")
        base, _, step_raw = part.partition("/")
        step = 1
        if step_raw:
            if not step_raw.isdigit() or int(step_raw) < 1:
                raise ValueError(f"Invalid step '/{step_raw}' in cron {label} field")
            step = int(step_raw)
        if base in {"*", "?"}:
            start, end = low, high
        elif "-" in base:
            start_raw, end_raw = base.split("-", 1)
            start = _parse_value(start_raw, names, label)
            end = _parse_value(end_raw, names, label)
        else:
            start = _parse_value(base, names, label)
            end = high if step_raw else start
        if not (low <= start <= high and low <= end <= high):
            raise ValueError(
                f"Cron {label} values must be between {low} and {high}"
            )
        if start > end:
            raise ValueError(f"Invalid range '{base}' in cron {label} field")
        values.update(range(start, end + 1, step))
    return values


def parse_cron(expression: str) -> CronSpec:
    """Parse ``minute hour day-of-month month day-of-week`` or a macro."""
    text = " ".join(expression.split())
    text = _MACROS.get(text.lower(), text)
    fields = text.split(" ")
    if len(fields) != 5:
        raise ValueError(
            "Cron expressions need 5 fields: minute hour day-of-month month day-of-week"
        )
    minute, hour, dom, month, dow = fields
    weekdays = _parse_field(dow, 0, 7, "day-of-week", _DAY_NAMES)
    if 7 in weekdays:
        weekdays = (weekdays - {7}) | {0}
    return CronSpec(
        minutes=tuple(sorted(_parse_field(minute, 0, 59, "minute"))),
        hours=tuple(sorted(_parse_field(hour, 0, 23, "hour"))),
        days=frozenset(_parse_field(dom, 1, 31, "day-of-month")),
        months=frozenset(_parse_field(month, 1, 12, "month", _MONTH_NAMES)),
        weekdays=frozenset(weekdays),
        day_restricted=not dom.startswith(("*", "?")),
        weekday_restricted=not dow.startswith(("*", "?")),
    )


def split_cron_expressions(value: str) -> list[str]:
    """Split ``;``/newline separated expressions into normalized parts."""
    return [
        " ".join(part.split())
        for part in re.split(r"[;\n]", value)
        if part.strip()
    ]


def normalize_cron_expression(value: object) -> str:
    """Validate one or more cron expressions and return the canonical text."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("cron_expression is required for cron recurrence")
    if len(value) > MAX_CRON_LENGTH:
        raise ValueError(
            f"cron_expression must be at most {MAX_CRON_LENGTH} characters"
        )
    parts = split_cron_expressions(value)
    if len(parts) > MAX_CRON_EXPRESSIONS:
        raise ValueError(
            f"Use at most {MAX_CRON_EXPRESSIONS} cron expressions per task"
        )
    specs = [parse_cron(part) for part in parts]
    today = datetime.now(timezone.utc).date()
    for part, spec in zip(parts, specs):
        if not any(
            spec.matches_date(today + timedelta(days=offset))
            for offset in range(_CRON_SEARCH_DAYS)
        ):
            raise ValueError(f"Cron expression '{part}' never matches a date")
    return "; ".join(parts)


def _first_instant(
    day: date, at: time, zone: ZoneInfo, after_utc: datetime
) -> datetime | None:
    naive = datetime.combine(day, at)
    candidate = naive.replace(tzinfo=zone, fold=0)
    instant = candidate.astimezone(timezone.utc)
    if instant.astimezone(zone).replace(tzinfo=None) != naive:
        return None  # Nonexistent wall time (spring-forward gap).
    return instant if instant > after_utc else None


def _next_cron_occurrence(
    after: datetime, expression: str, zone: ZoneInfo
) -> datetime:
    specs = [parse_cron(part) for part in split_cron_expressions(expression)]
    if not specs:
        raise ValueError("cron_expression is required for cron recurrence")
    after_utc = after.astimezone(timezone.utc)
    current = after.astimezone(zone).date()
    for offset in range(_CRON_SEARCH_DAYS):
        day = current + timedelta(days=offset)
        best: datetime | None = None
        for spec in specs:
            if not spec.matches_date(day):
                continue
            for hour in spec.hours:
                found = None
                for minute in spec.minutes:
                    found = _first_instant(day, time(hour, minute), zone, after_utc)
                    if found is not None:
                        break
                if found is not None:
                    if best is None or found < best:
                        best = found
                    break
        if best is not None:
            return best
    raise ValueError("Could not calculate next occurrence")


def next_occurrence(
    *,
    after: datetime,
    local_time: time,
    timezone_name: str,
    recurrence: str,
    weekdays: list[int] | tuple[int, ...] = (),
    cron_expression: str = "",
) -> datetime:
    """Find the next valid local occurrence strictly after an instant.

    Nonexistent wall times are skipped. Ambiguous times select fold=0, so the
    occurrence is run once at the first instant matching that wall time.
    """
    if after.tzinfo is None:
        raise ValueError("after must be timezone-aware")
    zone = valid_timezone(timezone_name)
    if recurrence == "cron":
        return _next_cron_occurrence(after, cron_expression, zone)
    if recurrence not in {"daily", "weekly"}:
        raise ValueError("recurrence must be daily, weekly or cron")
    days = sorted(set(weekdays))
    if recurrence == "weekly" and (
        not days or any(day not in range(7) for day in days)
    ):
        raise ValueError("weekly recurrence requires weekdays from 0 (Mon) to 6 (Sun)")
    if recurrence == "daily" and days:
        raise ValueError("daily recurrence does not accept weekdays")

    current = after.astimezone(zone).date()
    # Seven days finds at least one weekly weekday; extra space handles DST gaps.
    for offset in range(0, 15):
        day = current + timedelta(days=offset)
        if recurrence == "weekly" and day.weekday() not in days:
            continue
        naive = datetime.combine(day, local_time.replace(tzinfo=None))
        candidate = naive.replace(tzinfo=zone, fold=0)
        instant = candidate.astimezone(timezone.utc)
        roundtrip = instant.astimezone(zone)
        if roundtrip.replace(tzinfo=None) != naive:
            continue
        if instant > after.astimezone(timezone.utc):
            return instant
    raise ValueError("Could not calculate next occurrence")
