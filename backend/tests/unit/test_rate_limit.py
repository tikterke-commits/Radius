from app.policy.rate_limit import RateSpec, format_rate_limit, parse_rate_limit, scale_to


def test_plain_rate_is_rx_slash_tx_only():
    assert format_rate_limit(RateSpec(rx=5_000_000, tx=20_000_000)) == "5000000/20000000"


def test_priority_alone_zero_fills_burst_fields():
    assert format_rate_limit(RateSpec(rx=1, tx=2, priority=5)) == "1/2 0/0 0/0 0/0 5"


def test_full_spec_round_trips():
    spec = RateSpec(
        rx=5_000_000, tx=20_000_000,
        burst_rx=8_000_000, burst_tx=30_000_000,
        threshold_rx=4_000_000, threshold_tx=15_000_000,
        burst_time_s=16, priority=3,
        min_rx=1_000_000, min_tx=4_000_000,
    )
    s = format_rate_limit(spec)
    assert s == "5000000/20000000 8000000/30000000 4000000/15000000 16/16 3 1000000/4000000"
    assert parse_rate_limit(s) == spec


def test_burst_without_threshold_gets_75pct_and_8s_defaults():
    s = format_rate_limit(RateSpec(rx=4_000_000, tx=8_000_000, burst_rx=6_000_000, burst_tx=12_000_000))
    assert s == "4000000/8000000 6000000/12000000 3000000/6000000 8/8"


def test_burst_not_above_max_is_dropped():
    # A burst <= max-limit does nothing in RouterOS; don't emit it.
    assert format_rate_limit(RateSpec(rx=10, tx=10, burst_rx=10, burst_tx=5)) == "10/10"


def test_min_without_priority_defaults_priority_8():
    assert format_rate_limit(RateSpec(rx=10, tx=20, min_rx=1, min_tx=2)) == "10/20 0/0 0/0 0/0 8 1/2"


def test_scale_keeps_shape_and_clamps_limit_at():
    spec = RateSpec(rx=8, tx=40, burst_rx=16, burst_tx=80, threshold_rx=6, threshold_tx=30, min_rx=4, min_tx=20)
    scaled = scale_to(spec, rx=2, tx=10)  # 25% peak
    assert (scaled.burst_rx, scaled.burst_tx) == (4, 20)
    assert (scaled.threshold_rx, scaled.threshold_tx) == (1, 7)
    assert (scaled.min_rx, scaled.min_tx) == (2, 10)  # clamped to new max


def test_parse_accepts_routeros_suffixes():
    assert parse_rate_limit("2M/10M") == RateSpec(rx=2_000_000, tx=10_000_000)
    assert parse_rate_limit("512k/1M").rx == 512_000
