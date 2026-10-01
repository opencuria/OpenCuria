from datetime import datetime, time, timezone

import pytest

from apps.scheduled_tasks.recurrence import next_occurrence


def test_daily_uses_local_wall_time_across_spring_dst():
    after = datetime(2025, 3, 8, 15, tzinfo=timezone.utc)
    result = next_occurrence(
        after=after,
        local_time=time(9),
        timezone_name="America/New_York",
        recurrence="daily",
    )
    assert result == datetime(2025, 3, 9, 13, tzinfo=timezone.utc)


def test_nonexistent_spring_time_is_skipped():
    after = datetime(2025, 3, 8, 8, tzinfo=timezone.utc)
    result = next_occurrence(
        after=after,
        local_time=time(2, 30),
        timezone_name="America/New_York",
        recurrence="daily",
    )
    assert result == datetime(2025, 3, 10, 6, 30, tzinfo=timezone.utc)


def test_ambiguous_fall_time_occurs_once_at_first_fold():
    after = datetime(2025, 11, 2, 4, tzinfo=timezone.utc)
    result = next_occurrence(
        after=after,
        local_time=time(1, 30),
        timezone_name="America/New_York",
        recurrence="daily",
    )
    assert result == datetime(2025, 11, 2, 5, 30, tzinfo=timezone.utc)


def test_weekdays_follow_iso_weekday_numbers():
    after = datetime(2025, 1, 6, 10, tzinfo=timezone.utc)  # Monday
    result = next_occurrence(
        after=after,
        local_time=time(9),
        timezone_name="UTC",
        recurrence="weekly",
        weekdays=[0, 2, 4],
    )
    assert result == datetime(2025, 1, 8, 9, tzinfo=timezone.utc)


def test_invalid_timezone_rejected():
    with pytest.raises(ValueError, match="Unknown IANA timezone"):
        next_occurrence(
            after=datetime.now(timezone.utc),
            local_time=time(9),
            timezone_name="not/a-zone",
            recurrence="daily",
        )
