from datetime import datetime, timedelta

from app.policy.gates import SubscriberSnapshot, evaluate_gates

NOW = datetime(2026, 8, 17, 12, 0, 0)


def snapshot(**overrides) -> SubscriberSnapshot:
    defaults = dict(
        enabled=True,
        expiration=NOW + timedelta(days=10),
        quota_rx_remaining=1_000_000,
        quota_tx_remaining=1_000_000,
        quota_rxtx_remaining=2_000_000,
        quota_uptime_remaining_s=3600,
        now=NOW,
    )
    defaults.update(overrides)
    return SubscriberSnapshot(**defaults)


def test_all_gates_pass_for_healthy_subscriber():
    result = evaluate_gates(snapshot())
    assert result.all_pass
    assert result.failed_gates() == []


def test_disabled_account_fails_only_status_gate():
    result = evaluate_gates(snapshot(enabled=False))
    assert not result.all_pass
    assert result.failed_gates() == ["status"]
    # the other three gates are independently still true — this is the point
    # of the four-gate model over a single boolean
    assert result.traffic_gate
    assert result.expiration_gate
    assert result.uptime_gate


def test_expired_subscription_fails_only_expiration_gate():
    result = evaluate_gates(snapshot(expiration=NOW - timedelta(days=1)))
    assert result.failed_gates() == ["expiration"]


def test_exhausted_rx_quota_fails_traffic_gate():
    result = evaluate_gates(snapshot(quota_rx_remaining=0))
    assert result.failed_gates() == ["traffic"]


def test_exhausted_rxtx_quota_fails_traffic_gate_even_if_rx_and_tx_remain():
    result = evaluate_gates(snapshot(quota_rxtx_remaining=0))
    assert result.failed_gates() == ["traffic"]


def test_exhausted_uptime_fails_only_uptime_gate():
    result = evaluate_gates(snapshot(quota_uptime_remaining_s=0))
    assert result.failed_gates() == ["uptime"]


def test_unlimited_quota_is_none_and_always_passes():
    result = evaluate_gates(
        snapshot(
            quota_rx_remaining=None,
            quota_tx_remaining=None,
            quota_rxtx_remaining=None,
            quota_uptime_remaining_s=None,
        )
    )
    assert result.traffic_gate
    assert result.uptime_gate


def test_multiple_gates_can_fail_simultaneously():
    result = evaluate_gates(
        snapshot(enabled=False, expiration=NOW - timedelta(days=1), quota_rx_remaining=0)
    )
    assert set(result.failed_gates()) == {"status", "expiration", "traffic"}


def test_expiration_exactly_now_fails_gate():
    # boundary: expiration == now means it has expired, not "expires this instant"
    result = evaluate_gates(snapshot(expiration=NOW))
    assert result.failed_gates() == ["expiration"]
