from datetime import datetime, time

from app.policy.time_of_day import TimeWindow, apply_time_of_day, effective_multiplier

# The prompt's concrete example: international capped to 25% during 5pm-midnight peak
PEAK_INTERNATIONAL = TimeWindow(
    start=time(17, 0),
    end=time(23, 59, 59, 999999),
    days=frozenset(["mon", "tue", "wed", "thu", "fri", "sat", "sun"]),
    speed_multiplier=0.25,
    applies_to="international",
)


def test_no_windows_means_full_rate():
    assert effective_multiplier([], datetime(2026, 8, 17, 20, 0), "international") == 1.0


def test_window_applies_during_peak_for_matching_class():
    now = datetime(2026, 8, 17, 20, 0)  # 8pm, within 17:00-24:00
    assert effective_multiplier([PEAK_INTERNATIONAL], now, "international") == 0.25


def test_window_does_not_apply_outside_peak_hours():
    now = datetime(2026, 8, 17, 10, 0)  # 10am
    assert effective_multiplier([PEAK_INTERNATIONAL], now, "international") == 1.0


def test_window_does_not_apply_to_non_matching_traffic_class():
    now = datetime(2026, 8, 17, 20, 0)
    assert effective_multiplier([PEAK_INTERNATIONAL], now, "local") == 1.0


def test_wraps_past_midnight():
    window = TimeWindow(
        start=time(22, 0),
        end=time(2, 0),
        days=frozenset(["mon"]),
        speed_multiplier=0.5,
        applies_to="*",
    )
    # Monday 23:00 -> inside the window, started today
    assert effective_multiplier([window], datetime(2026, 8, 17, 23, 0), "x") == 0.5
    # Tuesday 01:00 -> still inside the window, which started Monday night
    assert effective_multiplier([window], datetime(2026, 8, 18, 1, 0), "x") == 0.5
    # Tuesday 03:00 -> window has ended
    assert effective_multiplier([window], datetime(2026, 8, 18, 3, 0), "x") == 1.0
    # Wednesday 23:00 -> Tuesday isn't a configured start day, no match
    assert effective_multiplier([window], datetime(2026, 8, 19, 23, 0), "x") == 1.0


def test_most_restrictive_window_wins_on_overlap():
    windows = [
        TimeWindow(time(17, 0), time(23, 59, 59), frozenset(["mon"]), 0.5, "*"),
        TimeWindow(time(18, 0), time(20, 0), frozenset(["mon"]), 0.1, "*"),
    ]
    now = datetime(2026, 8, 17, 19, 0)  # Monday 7pm, inside both windows
    assert effective_multiplier(windows, now, "x") == 0.1


def test_apply_time_of_day_scales_both_directions():
    rx, tx = apply_time_of_day(100_000_000, 100_000_000, [PEAK_INTERNATIONAL], datetime(2026, 8, 17, 20, 0), "international")
    assert (rx, tx) == (25_000_000, 25_000_000)
