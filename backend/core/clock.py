"""Time, timezones, and quiet hours.

Quiet hours are a product safety requirement, so the whole calculation lives
here as pure functions over an injected ``now`` -- no hidden calls to
``datetime.now()`` anywhere in the enforcement path, which is what makes it
testable.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from backend.core.config import QUIET_HOURS_END, QUIET_HOURS_START

UTC = UTC
DEFAULT_TIMEZONE = "UTC"


def now_utc() -> datetime:
    return datetime.now(tz=UTC)


def iso(dt: datetime) -> str:
    """RFC3339 in UTC, always with a trailing Z. Sorts lexicographically.

    Microsecond precision, not milliseconds: these strings are DynamoDB sort
    keys, and a coarser clock lets an inbound message and the reply it
    triggered land in the same tick and then order arbitrarily.
    """
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def tz_for(name: str | None) -> ZoneInfo:
    """Resolve a player's timezone, falling back to UTC rather than raising.

    A bad timezone string must never be able to stop the story or, worse,
    silently disable quiet hours.
    """
    try:
        return ZoneInfo(name or DEFAULT_TIMEZONE)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_TIMEZONE)


def to_local(dt: datetime, tz_name: str | None) -> datetime:
    return dt.astimezone(tz_for(tz_name))


def local_date_key(dt: datetime, tz_name: str | None) -> str:
    """The player-local calendar day, used to bucket daily rate limits."""
    return to_local(dt, tz_name).strftime("%Y-%m-%d")


def in_quiet_hours(dt: datetime, tz_name: str | None) -> bool:
    """True when player-local time falls in [22:00, 08:00).

    The window wraps midnight, so this is an OR, not an AND.
    """
    hour = to_local(dt, tz_name).hour
    return hour >= QUIET_HOURS_START or hour < QUIET_HOURS_END


def next_allowed_time(dt: datetime, tz_name: str | None) -> datetime:
    """The first instant at or after ``dt`` that is outside quiet hours.

    Returned in UTC. Callers that are blocked by quiet hours use this to
    reschedule rather than to drop the beat.
    """
    if not in_quiet_hours(dt, tz_name):
        return dt.astimezone(UTC)

    local = to_local(dt, tz_name)
    target = local.replace(hour=QUIET_HOURS_END, minute=0, second=0, microsecond=0)
    if local.hour >= QUIET_HOURS_START:
        # Late evening: the next 08:00 is tomorrow's.
        target += timedelta(days=1)
    return target.astimezone(UTC)


def ttl_epoch(dt: datetime, days: int) -> int:
    """DynamoDB TTL value: whole seconds since the epoch."""
    return int((dt + timedelta(days=days)).timestamp())
