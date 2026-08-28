"""Quiet hours and player-local day boundaries.

The window wraps midnight and is evaluated in the player's timezone, which is
the pair of details most likely to be got wrong silently.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from backend.core.clock import (
    arc_day,
    in_quiet_hours,
    local_date_key,
    next_allowed_time,
    to_local,
    tz_for,
)


def at(hour: int, minute: int = 0, day: int = 10) -> datetime:
    return datetime(2026, 3, day, hour, minute, tzinfo=UTC)


@pytest.mark.parametrize(
    ("hour", "quiet"),
    [
        (0, True),
        (3, True),
        (7, True),
        (7.99, True),
        (8, False),
        (9, False),
        (12, False),
        (21, False),
        (21.99, False),
        (22, True),
        (23, True),
    ],
)
def test_quiet_hours_boundaries_in_utc(hour: float, quiet: bool) -> None:
    dt = at(int(hour), 59 if hour % 1 else 0)
    assert in_quiet_hours(dt, "UTC") is quiet


def test_quiet_hours_are_evaluated_in_the_players_timezone() -> None:
    """23:00 UTC is the middle of the night in London and mid-evening in Denver."""
    dt = at(23)
    assert in_quiet_hours(dt, "Europe/London") is True
    assert in_quiet_hours(dt, "America/Denver") is False  # 17:00 local


def test_an_unknown_timezone_falls_back_to_utc_rather_than_raising() -> None:
    """A bad timezone string must never disable quiet hours."""
    assert tz_for("Not/AZone").key == "UTC"
    assert tz_for(None).key == "UTC"
    assert in_quiet_hours(at(2), "Not/AZone") is True


def test_next_allowed_time_from_late_evening_is_the_following_morning() -> None:
    blocked = at(23, 30)  # 23:30 UTC
    allowed = next_allowed_time(blocked, "UTC")
    assert allowed.date().day == 11
    assert to_local(allowed, "UTC").hour == 8


def test_next_allowed_time_from_early_morning_is_the_same_day() -> None:
    blocked = at(3, 15)
    allowed = next_allowed_time(blocked, "UTC")
    assert allowed.date().day == 10
    assert to_local(allowed, "UTC").hour == 8


def test_next_allowed_time_outside_quiet_hours_is_now() -> None:
    fine = at(14)
    assert next_allowed_time(fine, "UTC") == fine


def test_next_allowed_time_respects_the_players_timezone() -> None:
    """23:30 UTC is 16:30 in Denver -- nothing to defer."""
    dt = at(23, 30)
    assert next_allowed_time(dt, "America/Denver") == dt


def test_daily_quota_buckets_use_the_players_local_day() -> None:
    """01:00 UTC is still the previous day in Denver, so the counter must not roll."""
    dt = datetime(2026, 3, 11, 1, 0, tzinfo=UTC)
    assert local_date_key(dt, "UTC") == "2026-03-11"
    assert local_date_key(dt, "America/Denver") == "2026-03-10"


def test_quiet_hours_follow_dst_not_a_fixed_offset() -> None:
    """The same UTC wall time straddles the quiet-hours boundary across DST.

    US DST began on 2026-03-08. At 12:30 UTC New York is 07:30 the day before
    (EST, UTC-5) and 08:30 the day after (EDT, UTC-4) -- so an identical clock
    reading is inside quiet hours on one date and outside it on the other. A
    hard-coded offset would get exactly one of these wrong.
    """
    est = datetime(2026, 3, 7, 12, 30, tzinfo=UTC)
    edt = datetime(2026, 3, 9, 12, 30, tzinfo=UTC)

    assert to_local(est, "America/New_York").hour == 7
    assert to_local(edt, "America/New_York").hour == 8

    assert in_quiet_hours(est, "America/New_York") is True
    assert in_quiet_hours(edt, "America/New_York") is False


# ------------------------------------------------------------- arc days


def test_the_arc_starts_on_day_one() -> None:
    start = at(15)
    assert arc_day(start, start, "Europe/London") == 1


def test_a_day_is_a_local_calendar_day_not_twenty_four_hours() -> None:
    """Enrol at 23:30 and you are on day 2 half an hour later, as you would say."""
    start = datetime(2026, 3, 10, 23, 30, tzinfo=UTC)
    later = datetime(2026, 3, 11, 0, 5, tzinfo=UTC)
    assert arc_day(start, later, "Europe/London") == 2


def test_the_day_is_counted_in_the_players_timezone() -> None:
    """22:00 UTC is already tomorrow in Tokyo and still today in London."""
    start = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)
    later = datetime(2026, 3, 10, 22, 0, tzinfo=UTC)
    assert arc_day(start, later, "Europe/London") == 1
    assert arc_day(start, later, "Asia/Tokyo") == 2


def test_the_day_never_goes_below_one() -> None:
    """A clock skew must not produce day zero and a missing beat."""
    start = at(15, day=10)
    assert arc_day(start, at(15, day=8), "Europe/London") == 1


def test_a_week_of_silence_still_advances_the_day() -> None:
    start = at(15, day=10)
    assert arc_day(start, at(15, day=17), "Europe/London") == 8
