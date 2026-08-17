"""Time-of-day policy (A4): "international capped to 25% during the 5pm-midnight
peak" as a first-class, expressible-per-plan-and-per-subscriber primitive, not
a static router rule. A window matches on weekday + time-of-day + traffic
class, and applies a speed multiplier to whatever the base rate would
otherwise be. Per-subscriber exceptions (SubscriberPolicyException) can
override or exempt a subscriber from a window entirely.
"""

from dataclasses import dataclass
from datetime import datetime, time


WEEKDAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


@dataclass(frozen=True)
class TimeWindow:
    start: time
    end: time  # end < start means the window wraps past midnight
    days: frozenset[str]  # subset of WEEKDAY_NAMES
    speed_multiplier: float  # e.g. 0.25 for the 25% international cap
    applies_to: str  # traffic class tag, e.g. "international", "*"


def window_matches(window: TimeWindow, now: datetime, traffic_class: str) -> bool:
    if window.applies_to != "*" and window.applies_to != traffic_class:
        return False

    now_t = now.time()
    weekday = WEEKDAY_NAMES[now.weekday()]

    if window.start <= window.end:
        return weekday in window.days and window.start <= now_t < window.end

    # Wraps past midnight (e.g. 22:00-02:00): a window that "started Monday"
    # is still active in the small hours of Tuesday. So it matches if either
    # today is a configured start day and we're past the start time, or
    # yesterday was a configured start day and we're still before the end
    # time — checking weekday membership before considering the wrap (as a
    # naive same-day-only check would) silently drops that tail segment.
    prev_weekday = WEEKDAY_NAMES[(now.weekday() - 1) % 7]
    started_today = weekday in window.days and now_t >= window.start
    continuing_from_yesterday = prev_weekday in window.days and now_t < window.end
    return started_today or continuing_from_yesterday


def effective_multiplier(
    windows: list[TimeWindow], now: datetime, traffic_class: str
) -> float:
    """If multiple windows match, the most restrictive (lowest multiplier)
    wins — a policy conflict should never accidentally grant more bandwidth
    than intended.
    """
    matching = [w.speed_multiplier for w in windows if window_matches(w, now, traffic_class)]
    if not matching:
        return 1.0
    return min(matching)


def apply_time_of_day(
    base_rx_limit_bps: int,
    base_tx_limit_bps: int,
    windows: list[TimeWindow],
    now: datetime,
    traffic_class: str,
) -> tuple[int, int]:
    multiplier = effective_multiplier(windows, now, traffic_class)
    return int(base_rx_limit_bps * multiplier), int(base_tx_limit_bps * multiplier)
