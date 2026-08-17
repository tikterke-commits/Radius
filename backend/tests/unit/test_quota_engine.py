from app.policy.quota_engine import (
    AccountingDelta,
    FupTier,
    QuotaKind,
    QuotaLedger,
    QuotaPeriod,
    apply_fup,
    apply_usage,
)


def full_ledger(**overrides) -> QuotaLedger:
    defaults = dict(
        rx_monthly=10_000_000,
        tx_monthly=10_000_000,
        rxtx_monthly=20_000_000,
        uptime_monthly_s=100_000,
        rx_daily=1_000_000,
        tx_daily=1_000_000,
        rxtx_daily=2_000_000,
        uptime_daily_s=10_000,
    )
    defaults.update(overrides)
    return QuotaLedger(**defaults)


def test_apply_usage_decrements_all_eight_fields():
    ledger = full_ledger()
    delta = AccountingDelta(rx_bytes=100, tx_bytes=200, session_seconds=60)
    updated = apply_usage(ledger, delta)

    assert updated.rx_monthly == 10_000_000 - 100
    assert updated.tx_monthly == 10_000_000 - 200
    assert updated.rxtx_monthly == 20_000_000 - 300  # rx + tx
    assert updated.uptime_monthly_s == 100_000 - 60
    assert updated.rx_daily == 1_000_000 - 100
    assert updated.tx_daily == 1_000_000 - 200
    assert updated.rxtx_daily == 2_000_000 - 300
    assert updated.uptime_daily_s == 10_000 - 60


def test_apply_usage_clamps_at_zero_not_negative():
    ledger = full_ledger(rx_daily=50)
    delta = AccountingDelta(rx_bytes=1000, tx_bytes=0, session_seconds=0)
    updated = apply_usage(ledger, delta)
    assert updated.rx_daily == 0


def test_apply_usage_preserves_unlimited_none_fields():
    ledger = full_ledger(rx_monthly=None)
    delta = AccountingDelta(rx_bytes=500, tx_bytes=0, session_seconds=0)
    updated = apply_usage(ledger, delta)
    assert updated.rx_monthly is None


def test_is_exhausted_true_at_zero():
    ledger = full_ledger(rx_daily=0)
    assert ledger.is_exhausted(QuotaKind.RX, QuotaPeriod.DAILY)


def test_is_exhausted_false_for_unlimited():
    ledger = full_ledger(rx_daily=None)
    assert not ledger.is_exhausted(QuotaKind.RX, QuotaPeriod.DAILY)


def test_daily_exhaustion_independent_of_monthly():
    # a subscriber who blew today's daily cap but has plenty of monthly quota
    # left should still show daily exhausted, monthly fine
    ledger = full_ledger(rx_daily=0, rx_monthly=9_000_000)
    assert ledger.is_exhausted(QuotaKind.RX, QuotaPeriod.DAILY)
    assert not ledger.is_exhausted(QuotaKind.RX, QuotaPeriod.MONTHLY)


def test_apply_fup_no_tier_crossed_returns_base_rate():
    tiers = [FupTier(after_bytes=10_000_000_000, rx_limit_bps=1_000_000, tx_limit_bps=1_000_000)]
    rx, tx = apply_fup(20_000_000, 20_000_000, tiers, consumed_rxtx_bytes=0)
    assert (rx, tx) == (20_000_000, 20_000_000)


def test_apply_fup_selects_highest_crossed_tier():
    tiers = [
        FupTier(after_bytes=5_000_000_000, rx_limit_bps=10_000_000, tx_limit_bps=10_000_000),
        FupTier(after_bytes=10_000_000_000, rx_limit_bps=2_000_000, tx_limit_bps=2_000_000),
    ]
    # crossed both thresholds — should get the more restrictive (higher threshold) tier
    rx, tx = apply_fup(20_000_000, 20_000_000, tiers, consumed_rxtx_bytes=12_000_000_000)
    assert (rx, tx) == (2_000_000, 2_000_000)


def test_apply_fup_selects_only_crossed_tier():
    tiers = [
        FupTier(after_bytes=5_000_000_000, rx_limit_bps=10_000_000, tx_limit_bps=10_000_000),
        FupTier(after_bytes=10_000_000_000, rx_limit_bps=2_000_000, tx_limit_bps=2_000_000),
    ]
    rx, tx = apply_fup(20_000_000, 20_000_000, tiers, consumed_rxtx_bytes=6_000_000_000)
    assert (rx, tx) == (10_000_000, 10_000_000)
