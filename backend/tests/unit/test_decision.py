"""decide() is the whole authorize policy as a pure function — these cover each
stage of its documented evaluation order without a database."""

from datetime import datetime, time, timedelta, timezone

from app.policy.clock import to_local
from app.policy.decision import (
    DailyPolicySpec,
    FreeWindow,
    PlanSpec,
    PolicyInputs,
    SubscriberSpec,
    UsageSnapshot,
    decide,
    stable_attributes,
)
from app.policy.quota_engine import FupTier, QuotaKind, QuotaPeriod
from app.policy.rate_limit import RateSpec
from app.policy.time_of_day import WEEKDAY_NAMES, TimeWindow

ALL_DAYS = frozenset(WEEKDAY_NAMES)
NOW = datetime(2026, 12, 2, 16, 0, tzinfo=timezone.utc)  # 18:00 Beirut (winter, UTC+2)
GB = 1_000_000_000


def inputs(**kw) -> PolicyInputs:
    base = dict(
        subscriber=SubscriberSpec(),
        plan=PlanSpec(rate=RateSpec(rx=5_000_000, tx=20_000_000)),
        now=NOW,
        local_now=to_local(NOW, "Asia/Beirut"),
        expiration=NOW + timedelta(days=10),
    )
    base.update(kw)
    return PolicyInputs(**base)


def test_healthy_subscriber_gets_profile_rate():
    d = decide(inputs())
    assert d.accepted and d.reason == "ok"
    assert d.reply_attributes == {"Mikrotik-Rate-Limit": "5000000/20000000"}


def test_mac_binding_mismatch_rejects():
    d = decide(inputs(subscriber=SubscriberSpec(mac_binding="AA:BB:CC:00:00:01"), calling_station_id="aa:bb:cc:00:00:02"))
    assert not d.accepted and d.reason == "mac_binding_mismatch"


def test_mac_binding_is_case_insensitive():
    d = decide(inputs(subscriber=SubscriberSpec(mac_binding="AA:BB:CC:00:00:01"), calling_station_id="aa:bb:cc:00:00:01"))
    assert d.accepted


def test_expired_blocks_by_default():
    d = decide(inputs(expiration=NOW - timedelta(minutes=1)))
    assert not d.accepted and d.reason == "gate:expiration"


def test_expired_goes_to_walled_garden_when_profile_says_so():
    plan = PlanSpec(rate=RateSpec(rx=5_000_000, tx=20_000_000), on_expired="walled_garden")
    d = decide(inputs(plan=plan, expiration=NOW - timedelta(minutes=1)))
    assert d.accepted and d.reason == "ok:walled_garden:expiration"
    assert d.reply_attributes["Mikrotik-Address-List"] == "walled-garden"
    assert d.reply_attributes["Mikrotik-Rate-Limit"] == "256000/256000"


def test_quota_exhausted_throttles_when_configured():
    plan = PlanSpec(rate=RateSpec(rx=5_000_000, tx=20_000_000), on_quota_exhausted="throttle",
                    exhausted_rx_bps=512_000, exhausted_tx_bps=1_000_000)
    d = decide(inputs(plan=plan, quota_remaining={(QuotaKind.RXTX, QuotaPeriod.MONTHLY): 0}))
    assert d.accepted and d.reason == "ok:throttled"
    assert d.reply_attributes["Mikrotik-Rate-Limit"] == "512000/1000000"


def test_quota_exhausted_blocks_by_default():
    d = decide(inputs(quota_remaining={(QuotaKind.RXTX, QuotaPeriod.MONTHLY): 0}))
    assert not d.accepted and d.reason == "gate:traffic"


def test_daily_fup_tier_applies_on_todays_counted_usage():
    daily = DailyPolicySpec(tiers=(FupTier(after_bytes=5 * GB, rx_limit_bps=1_000_000, tx_limit_bps=4_000_000),))
    d = decide(inputs(daily=daily, usage=UsageSnapshot(today_counted_rx=1 * GB, today_counted_tx=5 * GB)))
    assert d.reply_attributes["Mikrotik-Rate-Limit"] == "1000000/4000000"
    assert "daily FUP tier" in d.explain


def test_daily_policy_counts_download_only_when_configured():
    daily = DailyPolicySpec(counted="tx", tiers=(FupTier(after_bytes=5 * GB, rx_limit_bps=1, tx_limit_bps=1),))
    d = decide(inputs(daily=daily, usage=UsageSnapshot(today_counted_rx=4 * GB, today_counted_tx=2 * GB)))
    assert d.reply_attributes["Mikrotik-Rate-Limit"] == "5000000/20000000"  # only 2 GB counted


def test_daily_cap_block():
    daily = DailyPolicySpec(daily_quota_bytes=10 * GB, on_exhaust="block")
    d = decide(inputs(daily=daily, usage=UsageSnapshot(today_counted_tx=10 * GB)))
    assert not d.accepted and d.reason == "daily_quota_exhausted"


def test_monthly_fup_uses_period_usage():
    plan = PlanSpec(rate=RateSpec(rx=5_000_000, tx=20_000_000),
                    fup_tiers=(FupTier(after_bytes=100 * GB, rx_limit_bps=2_000_000, tx_limit_bps=8_000_000),))
    d = decide(inputs(plan=plan, usage=UsageSnapshot(period_counted_tx=100 * GB)))
    assert d.reply_attributes["Mikrotik-Rate-Limit"] == "2000000/8000000"


def test_most_restrictive_of_monthly_and_daily_wins_per_direction():
    plan = PlanSpec(rate=RateSpec(rx=10, tx=100), fup_tiers=(FupTier(after_bytes=1, rx_limit_bps=2, tx_limit_bps=80),))
    daily = DailyPolicySpec(tiers=(FupTier(after_bytes=1, rx_limit_bps=6, tx_limit_bps=50),))
    d = decide(inputs(plan=plan, daily=daily, usage=UsageSnapshot(today_counted_tx=1, period_counted_tx=1)))
    assert d.reply_attributes["Mikrotik-Rate-Limit"] == "2/50"


def test_exempt_fup_ignores_tiers():
    daily = DailyPolicySpec(tiers=(FupTier(after_bytes=0, rx_limit_bps=1, tx_limit_bps=1),))
    d = decide(inputs(subscriber=SubscriberSpec(exempt_fup=True), daily=daily))
    assert d.reply_attributes["Mikrotik-Rate-Limit"] == "5000000/20000000"


def test_peak_window_all_traffic_scales_own_rate_in_local_time():
    # 17:00-02:00 local; NOW is 18:00 local but only 16:00 UTC — evaluating in
    # UTC would wrongly say "off-peak".
    peak = TimeWindow(start=time(17), end=time(2), days=ALL_DAYS, speed_multiplier=0.25, applies_to="all")
    d = decide(inputs(tod_windows=(peak,)))
    assert d.reply_attributes["Mikrotik-Rate-Limit"] == "1250000/5000000"


def test_peak_window_scales_burst_too():
    plan = PlanSpec(rate=RateSpec(rx=4_000_000, tx=8_000_000, burst_rx=8_000_000, burst_tx=16_000_000, burst_time_s=10))
    peak = TimeWindow(start=time(17), end=time(2), days=ALL_DAYS, speed_multiplier=0.5, applies_to="all")
    d = decide(inputs(plan=plan, tod_windows=(peak,)))
    assert d.reply_attributes["Mikrotik-Rate-Limit"].startswith("2000000/4000000 4000000/8000000")


def test_international_window_tags_address_list_without_touching_rate():
    peak = TimeWindow(start=time(17), end=time(0), days=ALL_DAYS, speed_multiplier=0.25, applies_to="international")
    d = decide(inputs(tod_windows=(peak,)))
    assert d.reply_attributes["Mikrotik-Rate-Limit"] == "5000000/20000000"
    assert d.reply_attributes["Mikrotik-Address-List"] == "international-peak"


def test_exempt_time_of_day():
    peak = TimeWindow(start=time(17), end=time(2), days=ALL_DAYS, speed_multiplier=0.25, applies_to="all")
    d = decide(inputs(subscriber=SubscriberSpec(exempt_time_of_day=True), tod_windows=(peak,)))
    assert d.reply_attributes["Mikrotik-Rate-Limit"] == "5000000/20000000"


def test_concurrency_limit():
    d = decide(inputs(other_live_sessions=1))
    assert not d.accepted and d.reason == "concurrent_limit"
    d2 = decide(inputs(other_live_sessions=1, subscriber=SubscriberSpec(concurrent_session_limit=2)))
    assert d2.accepted


def test_nas_attributes_static_ip_beats_pool():
    plan = PlanSpec(rate=RateSpec(rx=1, tx=1), ip_pool="pool-res", mikrotik_group="pppoe-default",
                    session_timeout_s=86400, idle_timeout_s=600, interim_interval_s=300)
    d = decide(inputs(plan=plan, subscriber=SubscriberSpec(static_ip="100.64.1.10")))
    r = d.reply_attributes
    assert r["Framed-IP-Address"] == "100.64.1.10" and "Framed-Pool" not in r
    assert (r["Mikrotik-Group"], r["Session-Timeout"], r["Idle-Timeout"], r["Acct-Interim-Interval"]) == (
        "pppoe-default", 86400, 600, 300)


def test_custom_attributes_cannot_override_core_and_subscriber_wins():
    plan = PlanSpec(rate=RateSpec(rx=1, tx=1), custom_attributes=(
        {"attribute": "Mikrotik-Rate-Limit", "value": "999M/999M"},
        {"attribute": "Mikrotik-Advertise-URL", "value": "http://plan"},
    ))
    sub = SubscriberSpec(custom_attributes=({"attribute": "Mikrotik-Advertise-URL", "value": "http://sub"},))
    d = decide(inputs(plan=plan, subscriber=sub))
    assert d.reply_attributes["Mikrotik-Rate-Limit"] == "1/1"
    assert d.reply_attributes["Mikrotik-Advertise-URL"] == "http://sub"


def test_nas_side_quota_limit_splits_gigawords_and_is_volatile():
    plan = PlanSpec(rate=RateSpec(rx=1, tx=1), enforce_quota_on_nas=True)
    remaining = 5 * (1 << 32) + 123
    d = decide(inputs(plan=plan, quota_remaining={(QuotaKind.RXTX, QuotaPeriod.MONTHLY): remaining}))
    assert d.reply_attributes["Mikrotik-Total-Limit"] == 123
    assert d.reply_attributes["Mikrotik-Total-Limit-Gigawords"] == 5
    assert "Mikrotik-Total-Limit" not in stable_attributes(d.reply_attributes)


def test_free_window_detection():
    daily = DailyPolicySpec(free_windows=(FreeWindow(start=time(2), end=time(8), days=ALL_DAYS),))
    assert daily.is_free_time(datetime(2026, 12, 2, 3, 0))
    assert not daily.is_free_time(datetime(2026, 12, 2, 9, 0))
