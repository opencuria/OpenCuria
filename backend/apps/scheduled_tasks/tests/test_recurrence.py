from datetime import date, datetime, time, timezone

import pytest

from apps.scheduled_tasks.recurrence import next_occurrence, normalize_cron_expression


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


def _cron(after, expression, zone="UTC"):
    return next_occurrence(
        after=after,
        local_time=time(9),
        timezone_name=zone,
        recurrence="cron",
        cron_expression=expression,
    )


def test_cron_runs_several_times_per_day_in_local_time():
    after = datetime(2025, 1, 6, 8, 10, tzinfo=timezone.utc)  # Mon 09:10 Berlin
    expression = "*/30 8-17 * * 1-5"
    first = _cron(after, expression, "Europe/Berlin")
    assert first == datetime(2025, 1, 6, 8, 30, tzinfo=timezone.utc)
    late = datetime(2025, 1, 10, 16, 45, tzinfo=timezone.utc)  # Fri 17:45 Berlin
    assert _cron(late, expression, "Europe/Berlin") == datetime(
        2025, 1, 13, 7, tzinfo=timezone.utc
    )


def test_cron_multiple_expressions_pick_earliest():
    after = datetime(2025, 1, 6, 9, 5, tzinfo=timezone.utc)
    assert _cron(after, "0 9,18 * * *; 30 14 * * *") == datetime(
        2025, 1, 6, 14, 30, tzinfo=timezone.utc
    )


def test_cron_names_macros_and_sunday_seven():
    after = datetime(2025, 1, 6, 10, tzinfo=timezone.utc)  # Monday
    sunday_noon = datetime(2025, 1, 12, 12, tzinfo=timezone.utc)
    assert _cron(after, "0 12 * * sun") == sunday_noon
    assert _cron(after, "0 12 * * 7") == sunday_noon
    assert _cron(after, "@hourly") == datetime(2025, 1, 6, 11, tzinfo=timezone.utc)
    assert _cron(after, "0 9 1 JAN-MAR *") == datetime(
        2025, 2, 1, 9, tzinfo=timezone.utc
    )


def test_cron_day_of_month_or_weekday_when_both_restricted():
    after = datetime(2025, 1, 6, 10, tzinfo=timezone.utc)  # Monday the 6th
    # Vixie cron: restricted day-of-month OR restricted day-of-week.
    assert _cron(after, "0 9 15 * 3") == datetime(2025, 1, 8, 9, tzinfo=timezone.utc)
    assert _cron(after, "0 9 15 * *") == datetime(2025, 1, 15, 9, tzinfo=timezone.utc)


def test_cron_skips_dst_gap_and_runs_fall_back_hour_once():
    spring = datetime(2025, 3, 30, 0, 50, tzinfo=timezone.utc)  # 01:50 CET
    assert _cron(spring, "*/30 * * * *", "Europe/Berlin") == datetime(
        2025, 3, 30, 1, tzinfo=timezone.utc
    )  # 03:00 CEST; 02:00/02:30 do not exist.
    fall = datetime(2025, 10, 26, 0, 45, tzinfo=timezone.utc)  # 02:45 CEST
    assert _cron(fall, "0 * * * *", "Europe/Berlin") == datetime(
        2025, 10, 26, 2, tzinfo=timezone.utc
    )  # 03:00 CET; the repeated 02:00 is not replayed.


def test_cron_leap_day_and_invalid_expressions():
    after = datetime(2025, 3, 1, tzinfo=timezone.utc)
    assert _cron(after, "0 0 29 2 *") == datetime(2028, 2, 29, tzinfo=timezone.utc)
    for bad in [
        "",
        "* * * *",
        "60 * * * *",
        "* 24 * * *",
        "*/0 * * * *",
        "5-1 * * * *",
        "0 0 * FOO *",
    ]:
        with pytest.raises(ValueError):
            normalize_cron_expression(bad)
    with pytest.raises(ValueError, match="never matches"):
        normalize_cron_expression("0 0 30 2 *")
    assert normalize_cron_expression(" 0  9 * * 1-5 ;\n30 14 * * *") == (
        "0 9 * * 1-5; 30 14 * * *"
    )


def test_once_returns_its_single_local_occurrence():
    after = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)
    result = next_occurrence(
        after=after,
        local_time=time(9),
        timezone_name="Europe/Berlin",
        recurrence="once",
        run_date=date(2026, 10, 25),
    )
    # 25 Oct 2026 is the fall-back day in Berlin; 09:00 is already CET.
    assert result == datetime(2026, 10, 25, 8, tzinfo=timezone.utc)


def test_once_ambiguous_fall_time_uses_first_fold():
    result = next_occurrence(
        after=datetime(2025, 11, 1, tzinfo=timezone.utc),
        local_time=time(1, 30),
        timezone_name="America/New_York",
        recurrence="once",
        run_date=date(2025, 11, 2),
    )
    assert result == datetime(2025, 11, 2, 5, 30, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("run_date", "local_time", "message"),
    [
        (None, time(9), "run_date is required"),
        (date(2025, 3, 9), time(2, 30), "does not exist"),
        (date(2025, 2, 28), time(9), "must be in the future"),
    ],
)
def test_once_rejects_missing_nonexistent_or_past_time(run_date, local_time, message):
    with pytest.raises(ValueError, match=message):
        next_occurrence(
            after=datetime(2025, 3, 1, 9, tzinfo=timezone.utc),
            local_time=local_time,
            timezone_name="America/New_York",
            recurrence="once",
            run_date=run_date,
        )
