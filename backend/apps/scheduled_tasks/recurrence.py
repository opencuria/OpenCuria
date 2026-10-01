"""Pure recurring schedule calculations with explicit DST policy."""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def valid_timezone(value: str) -> ZoneInfo:
    """Resolve an IANA zone or raise a user-facing validation error."""
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, TypeError) as exc:
        raise ValueError(f"Unknown IANA timezone: {value}") from exc


def next_occurrence(
    *,
    after: datetime,
    local_time: time,
    timezone_name: str,
    recurrence: str,
    weekdays: list[int] | tuple[int, ...] = (),
) -> datetime:
    """Find the next valid local occurrence strictly after an instant.

    Nonexistent wall times are skipped. Ambiguous times select fold=0, so the
    occurrence is run once at the first instant matching that wall time.
    """
    if after.tzinfo is None:
        raise ValueError("after must be timezone-aware")
    zone = valid_timezone(timezone_name)
    if recurrence not in {"daily", "weekly"}:
        raise ValueError("recurrence must be daily or weekly")
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
