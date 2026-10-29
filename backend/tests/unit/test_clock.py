from datetime import date, datetime, timezone

from app.policy.clock import month_start_utc, next_daily_reset, policy_day, to_local


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


def test_to_local_beirut_summer_is_utc_plus_3():
    assert to_local(utc(2026, 7, 1, 14, 0), "Asia/Beirut").hour == 17


def test_to_local_beirut_winter_is_utc_plus_2():
    assert to_local(utc(2026, 12, 1, 15, 0), "Asia/Beirut").hour == 17


def test_policy_day_with_midnight_reset_is_local_date():
    # 22:30 UTC on Oct 29 is 00:30 local on Oct 30 (UTC+2 after DST ends Oct 25 2026)
    assert policy_day(utc(2026, 10, 29, 22, 30), "Asia/Beirut", "00:00") == date(2026, 10, 30)


def test_policy_day_with_morning_reset_books_small_hours_to_previous_day():
    # 03:00 local on the 5th belongs to the 4th when the day rolls at 06:00
    assert policy_day(utc(2026, 12, 5, 1, 0), "Asia/Beirut", "06:00") == date(2026, 12, 4)


def test_next_daily_reset_is_next_local_midnight_in_utc():
    assert next_daily_reset(utc(2026, 12, 5, 10, 0), "Asia/Beirut", "00:00") == utc(2026, 12, 5, 22, 0)


def test_month_start_is_local_first_of_month():
    assert month_start_utc(utc(2026, 12, 15, 10, 0), "Asia/Beirut") == utc(2026, 11, 30, 22, 0)
