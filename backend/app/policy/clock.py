"""The policy clock. RADIUS and the database deal in UTC; every operator-facing
time rule (peak window, free hours, daily reset, calendar month) is local
time. Everything that evaluates a window converts here first — evaluating a
"17:00-02:00" window against a UTC clock silently shifts it by 2-3 hours
depending on DST, which is the bug the original authorize path had.

All functions take the zone / reset time explicitly (defaulting to settings)
so they're testable without monkeypatching config.
"""

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from app.config import settings


def tz(name: str | None = None) -> ZoneInfo:
    return ZoneInfo(name or settings.timezone)


def to_local(now: datetime, zone: str | None = None) -> datetime:
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(tz(zone))


def parse_hhmm(value: str) -> time:
    if value == "24:00":  # JSON convention for end-of-day
        return time(23, 59, 59, 999999)
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


def _reset_offset(reset_time: str | None) -> timedelta:
    t = parse_hhmm(reset_time or settings.daily_reset_time)
    return timedelta(hours=t.hour, minutes=t.minute)


def policy_day(now: datetime, zone: str | None = None, reset_time: str | None = None) -> date:
    """The local "day" usage is booked against. With a 00:00 reset this is just
    the local date; with e.g. 06:00, 03:00 on the 5th still belongs to the 4th.
    """
    return (to_local(now, zone) - _reset_offset(reset_time)).date()


def day_start_utc(day: date, zone: str | None = None, reset_time: str | None = None) -> datetime:
    """UTC instant at which policy day `day` begins."""
    offset = _reset_offset(reset_time)
    local_midnight = datetime.combine(day, time(0, 0), tzinfo=tz(zone))
    return (local_midnight + offset).astimezone(timezone.utc)


def next_daily_reset(now: datetime, zone: str | None = None, reset_time: str | None = None) -> datetime:
    today = policy_day(now, zone, reset_time)
    return day_start_utc(today + timedelta(days=1), zone, reset_time)


def month_key(now: datetime, zone: str | None = None) -> str:
    local = to_local(now, zone)
    return f"{local.year:04d}-{local.month:02d}"


def month_start_utc(now: datetime, zone: str | None = None) -> datetime:
    local = to_local(now, zone)
    start = datetime(local.year, local.month, 1, tzinfo=tz(zone))
    return start.astimezone(timezone.utc)


def next_month_start_utc(now: datetime, zone: str | None = None) -> datetime:
    local = to_local(now, zone)
    year, month = (local.year + 1, 1) if local.month == 12 else (local.year, local.month + 1)
    return datetime(year, month, 1, tzinfo=tz(zone)).astimezone(timezone.utc)
