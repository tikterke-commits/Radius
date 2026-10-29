"""Time-of-day policy (A4): "international capped to 25% during the 5pm-midnight
peak" as a first-class, expressible-per-plan-and-per-subscriber primitive, not
a static router rule. A window matches on weekday + time-of-day + traffic
class, and either scales / overrides the rate (applies_to "all") or tags the
subscriber into an address list for router-side shaping (any other class).

All functions here expect a LOCAL datetime — callers convert with
app.policy.clock.to_local first.
"""

from dataclasses import dataclass
from datetime import datetime, time

from app.policy.clock import parse_hhmm

WEEKDAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
ALL_TRAFFIC = ("all", "*")


@dataclass(frozen=True)
class TimeWindow:
    start: time
    end: time  # end < start means the window wraps past midnight
    days: frozenset[str]  # subset of WEEKDAY_NAMES
    speed_multiplier: float  # e.g. 0.25 for the 25% international cap; 1.0 = no scaling
    applies_to: str  # traffic class tag: "all" / "*" = the subscriber's own rate, else e.g. "international"
    rx_limit_bps: int | None = None  # absolute override (applies_to "all" only)
    tx_limit_bps: int | None = None
    address_list: str | None = None  # class windows: list the router keys off


def window_active(start: time, end: time, days: frozenset[str], now: datetime) -> bool:
    """Weekday + time-of-day match, including windows that wrap midnight."""
    now_t = now.time()
    weekday = WEEKDAY_NAMES[now.weekday()]

    if start <= end:
        return weekday in days and start <= now_t < end

    # Wraps past midnight (e.g. 22:00-02:00): a window that "started Monday"
    # is still active in the small hours of Tuesday. So it matches if either
    # today is a configured start day and we're past the start time, or
    # yesterday was a configured start day and we're still before the end
    # time — checking weekday membership before considering the wrap (as a
    # naive same-day-only check would) silently drops that tail segment.
    prev_weekday = WEEKDAY_NAMES[(now.weekday() - 1) % 7]
    started_today = weekday in days and now_t >= start
    continuing_from_yesterday = prev_weekday in days and now_t < end
    return started_today or continuing_from_yesterday


def window_matches(window: TimeWindow, now: datetime, traffic_class: str) -> bool:
    if window.applies_to not in ALL_TRAFFIC and window.applies_to != traffic_class:
        return False
    return window_active(window.start, window.end, window.days, now)


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


def own_rate_under_windows(
    rx_bps: int, tx_bps: int, windows: list[TimeWindow], now: datetime
) -> tuple[int, int, list[str]]:
    """Apply every active applies_to="all" window to the subscriber's own rate.
    Multipliers scale, absolute limits cap; most restrictive result wins per
    direction. Returns (rx, tx, explanations).
    """
    out_rx, out_tx = rx_bps, tx_bps
    notes: list[str] = []
    for w in windows:
        if w.applies_to not in ALL_TRAFFIC or not window_active(w.start, w.end, w.days, now):
            continue
        cand_rx, cand_tx = int(rx_bps * w.speed_multiplier), int(tx_bps * w.speed_multiplier)
        if w.rx_limit_bps is not None:
            cand_rx = min(cand_rx, w.rx_limit_bps)
        if w.tx_limit_bps is not None:
            cand_tx = min(cand_tx, w.tx_limit_bps)
        out_rx, out_tx = min(out_rx, cand_rx), min(out_tx, cand_tx)
        notes.append(f"time window {w.start:%H:%M}-{w.end:%H:%M} active")
    return out_rx, out_tx, notes


def class_address_lists(windows: list[TimeWindow], now: datetime) -> list[str]:
    """Address lists to tag for active traffic-class windows (applies_to other
    than "all"). A window tags only if it actually restricts something or
    names a list explicitly — a 1.0x class window is a no-op.
    """
    lists: list[str] = []
    for w in windows:
        if w.applies_to in ALL_TRAFFIC or not window_active(w.start, w.end, w.days, now):
            continue
        if w.address_list:
            name = w.address_list
        elif w.speed_multiplier < 1.0:
            name = f"{w.applies_to}-peak"
        else:
            continue
        if name not in lists:
            lists.append(name)
    return lists


def active_signature(windows: list[TimeWindow], now: datetime) -> str:
    """Stable string identifying which windows are active right now. The worker
    compares this tick-to-tick to detect window edges and push CoA."""
    return ",".join(
        str(i) for i, w in enumerate(windows) if window_active(w.start, w.end, w.days, now)
    )


def parse_window(raw: dict) -> TimeWindow:
    """The one deserialization point between TimeOfDayPolicy.windows JSON and
    the TimeWindow dataclass."""
    return TimeWindow(
        start=parse_hhmm(raw["start"]),
        end=parse_hhmm(raw["end"]),
        days=frozenset(raw.get("days") or WEEKDAY_NAMES),
        speed_multiplier=float(raw.get("speed_multiplier", 1.0)),
        applies_to=raw.get("applies_to", "all"),
        rx_limit_bps=raw.get("rx_limit_bps"),
        tx_limit_bps=raw.get("tx_limit_bps"),
        address_list=raw.get("address_list"),
    )
