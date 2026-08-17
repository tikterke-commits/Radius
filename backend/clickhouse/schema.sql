-- ClickHouse schema: append-only, high-volume, time-ordered data.
-- Mirrors docs/00-part-h-design.md §2/§4. Everything here is written
-- once-per-event and read in aggregate — never point-updated, which is
-- exactly the write shape MergeTree is built for and SQLite/WAL was not.

CREATE DATABASE IF NOT EXISTS radius_telemetry;

-- Real accounting history. Replaces radacct as a relational table (see
-- app/models/radius_fallback.py for why it's deliberately not there).
CREATE TABLE IF NOT EXISTS radius_telemetry.radacct_events
(
    event_ts        DateTime64(3) CODEC(Delta, ZSTD),
    subscriber_id   UUID,
    username        LowCardinality(String),
    nas_ip          IPv4,
    acct_session_id String,
    status_type     LowCardinality(String), -- Start / Interim-Update / Stop
    session_time_s  UInt32,
    input_octets    UInt64,   -- from the subscriber's perspective: what they sent (rx at the router)
    output_octets   UInt64,   -- what they received (tx at the router)
    terminate_cause LowCardinality(String) DEFAULT ''
)
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(event_ts)
ORDER BY (subscriber_id, event_ts)
TTL toDateTime(event_ts) + INTERVAL 1 YEAR;  -- raw kept ~1yr for billing disputes, see retention table in docs

-- SNMP subscriber telemetry (B1). Keyed on ifName, per the production
-- lesson: ifIndex is unstable for dynamic PPPoE interfaces.
CREATE TABLE IF NOT EXISTS radius_telemetry.snmp_samples
(
    sample_ts   DateTime64(3) CODEC(Delta, ZSTD),
    nas_ip      IPv4,
    if_name     LowCardinality(String),   -- == subscriber username on MikroTik PPPoE
    username    LowCardinality(String),
    rx_bytes_hc UInt64,  -- 64-bit HC counters required, per production lesson
    tx_bytes_hc UInt64
)
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(sample_ts)
ORDER BY (username, sample_ts)
TTL toDateTime(sample_ts) + INTERVAL 90 DAY;  -- rollup to hourly/daily aggregates before this, see retention policy

-- NetFlow/IPFIX, pre-NAT, ASN-enriched (B2). Short retention — this is for
-- live intelligence and incident investigation, not long-term analytics.
CREATE TABLE IF NOT EXISTS radius_telemetry.flow_records
(
    flow_ts     DateTime64(3) CODEC(Delta, ZSTD),
    subscriber_id Nullable(UUID),
    src_ip      IPv4,
    dst_ip      IPv4,
    src_port    UInt16,
    dst_port    UInt16,
    protocol    UInt8,
    asn         UInt32,
    bytes       UInt64,
    packets     UInt32
)
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(flow_ts)
ORDER BY (subscriber_id, flow_ts)
TTL toDateTime(flow_ts) + INTERVAL 14 DAY;

-- CGNAT lawful ledger (B3). Hash-chained, tamper-evident, append-only.
-- Retention here MUST be set from actual legal requirement, not left at
-- this placeholder — see docs/00-part-h-design.md §2.
CREATE TABLE IF NOT EXISTS radius_telemetry.lawful_ledger
(
    entry_ts        DateTime64(3) CODEC(Delta, ZSTD),
    subscriber_id   UUID,
    nas_ip          IPv4,
    private_ip      IPv4,
    private_port    UInt16,
    public_ip       IPv4,
    public_port     UInt16,
    dst_ip          IPv4,
    dst_port        UInt16,
    event_type      LowCardinality(String),  -- nat_create / nat_destroy / session_handover
    hash_prev       FixedString(64),         -- sha256 hex of the previous entry's hash_self
    hash_self       FixedString(64)          -- sha256 hex of (hash_prev || this row's canonical fields)
    -- Verification procedure: docs/lawful-ledger-verification.md (written alongside the
    -- actual hashing implementation, not before — a doc describing an unwritten algorithm
    -- is worse than no doc).
)
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(entry_ts)
ORDER BY (subscriber_id, entry_ts);
-- No TTL: indefinite by default until legal retention requirement is confirmed and set explicitly.

-- Security/IDS observations (B6, v1.2 per docs/00-part-h-design.md §1 sequencing).
CREATE TABLE IF NOT EXISTS radius_telemetry.security_observations
(
    observed_ts         DateTime64(3) CODEC(Delta, ZSTD),
    tier                UInt8,             -- 0, 1, 2
    detector             LowCardinality(String),
    subscriber_id        Nullable(UUID),
    bps_out               Nullable(Float64),
    threshold_bps         Nullable(Float64),  -- static floor, e.g. 5_000_000
    baseline_bps_out_p95  Nullable(Float64),  -- per-subscriber dynamic baseline
    detail                String DEFAULT ''
)
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(observed_ts)
ORDER BY (tier, observed_ts)
TTL toDateTime(observed_ts) + INTERVAL 180 DAY;
