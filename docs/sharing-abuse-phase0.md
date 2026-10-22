# Sharing & Abuse — Phase 0 Discovery Report

Status: **STOPPED, waiting for approval.** No code has been written.

## TL;DR — two blockers before anything else

1. **This repository is not Atlas.** `tikterke-commits/Radius` is the greenfield
   Data Plus platform (FastAPI + Postgres + ClickHouse + Redis + FreeRADIUS).
   It has no MySQL mirror, `flow_log`, `nat_log`, IPFIX collector, Unbound
   integration, pyasn, IDS incident queue or NOC UI. `docs/00-part-h-design.md` §0
   already records that the Atlas source tree (`/opt/dpc`) was never available
   here. `list_repos` shows `Radius` is the only repository this session can
   reach. The brief says to "extend the existing codebase, do NOT scaffold".
   That can't be done until the Atlas source is in a repo this session can read.
2. **There is no network path to production.** A TCP connect to `10.140.140.9:22`
   from this session failed. Items 1–6 below need live captures (IPFIX template,
   sample records, a SYN test, Unbound config, datastore load). Those have to be
   run on-site. This report gives the exact commands and what to look for.

**Decision needed:** (a) push the Atlas `/opt/dpc` source to a GitHub repo and
attach it to this session, or (b) build the module in this repo on its
ClickHouse `flow_records` / `security_observations` tables, then port it to
Atlas later.

---

## 1. Exporter and RouterOS version

- **From the repo:** `backend/app/models/nas.py` records the BNG as **ROS6
  6.49.19**. A ROS7 box is also mentioned (`supports_ip_accounting: false`).
  Which one is the PPPoE concentrator for the ~650 subscribers is not recorded.
- **ROS6:** `/ip traffic-flow` exports v1/v5/v9/IPFIX. To my knowledge, the
  6.x manual lists an `/ip traffic-flow ipfix` field menu that includes `ttl`,
  `tcp-window-size`, `ip-total-length`, `ip-header-length`, `tcp-flags`,
  `in-interface`, `first-forwarded`/`last-forwarded` and `nat-src-address`.
  This must be checked on 6.49.19 with `/ip traffic-flow ipfix print`. If those
  fields are missing, Methods A and D are blocked on that box.
- **Note:** MikroTik names the timestamps `first-forwarded`/`last-forwarded`,
  not `first/last-switched`. Verify all field names on the installed version.
- **Run on each PPPoE server:**
  ```
  /system resource print
  /ip traffic-flow print
  /ip traffic-flow target print detail
  /ip traffic-flow ipfix print
  ```
  - `target version` must be `9` or `ipfix`. v5 has no TTL.
  - `interfaces` must cover the PPPoE side. Export is ingress-based, so
    upstream flows come from `in-interface=<pppoe-user>`.
  - `active-flow-timeout` and `inactive-flow-timeout` decide how long flows
    run. This matters for concurrency (Method B).

## 2. Live template — capture procedure

On the collector host:
```
tcpdump -ni any udp port <collector_port> -c 2000 -w /tmp/ipfix.pcap
tshark -r /tmp/ipfix.pcap -d udp.port==<port>,cflow -V | less
```
- Template set: IPFIX set ID 2 (v9 flowset ID 0). Record which IEs are present:
  - 192 `ipTTL`
  - 186 `tcpWindowSize`
  - 224 `ipTotalLength`
  - 6 `tcpControlBits`
  - 10 `ingressInterface`
  - 152/153 or 21/22 flow start/end
- Then dump 50 data records. A field counts as "really populated" only if it
  varies across records. A constant 0 means the field is not implemented.
- Keep the pcap. It becomes the unit-test fixture.

## 3. Per-packet field semantics — test plan

These are undocumented on MikroTik, so treat them as unknown until tested.
- **Which packet:**
  - From a test PPPoE client, run `hping3 -S -p 443 -c 1 --ttl 50 <ext>`, then a
    full `curl` to the same host from the same source port.
  - If the record shows TTL ≈ 49/50, window = hping's, and length 40, the
    fields come from the first packet.
  - If they show curl's later-packet values, they come from the last packet.
  - Repeat with a long download to see whether the values change on
    active-timeout re-exports.
- **TTL decrement:** send TTL=64 from a host directly on the PPPoE client (no
  NAT in between).
  - 64 recorded → value as received (pre-decrement).
  - 63 recorded → post-routing.
  - Every Method A threshold depends on this offset.
- **TCP flags:** on most exporters, including MikroTik as far as I know, flags
  are the **cumulative OR** over the flow.
  - "SYN and not ACK" will almost never appear on a completed flow.
  - Method D therefore needs either first-packet semantics from the test above,
    or a proxy: flows whose window and length match a SYN shape.
  - If neither holds, Method D falls back to `(ttl_family, window)` only, which
    is much weaker.
- **MSS clamping:** `change-mss` rewrites the MSS value only. The options
  length, and so the SYN total length, stays the same. Confirm with a capture
  on both sides of the BNG.

## 4. Attribution path

- **Option A — `ingressInterface` → SNMP ifName = username.**
  - Exact, and survives IP reuse.
  - ifIndex for dynamic `<pppoe-…>` interfaces is reassigned on reconnect.
    The ifIndex→ifName map must be refreshed on every session start, or flows
    in the window after a reconnect go to the wrong user.
  - Existing Atlas lesson (and this repo's `snmp_samples` comment): key on
    ifName, never on a cached ifIndex.
- **Option B — `src IP` → active session `Framed-IP-Address` at flow time.**
  - Works without SNMP.
  - Fails during the ~seconds of IP handover between sessions. It also fails
    on flows that started under the previous session.
- **Recommendation:**
  - Primary: B with time-interval join on `(framed_ip, session_start..stop)`
    from accounting.
  - Cross-check: A.
  - Drop flows whose start predates the session. Measure the disagreement rate
    on one day of data before going live.

## 5. DNS visibility (Unbound, LXC 112)

Check on the container:
```
unbound -V | grep -i dnstap          # "--enable-dnstap" in configure line?
unbound-control get_option log-queries
unbound-control stats_noreset | grep total.num.queries
grep -rn 'dnstap\|access-control\|interface' /etc/unbound/
```
- **Client IP:** the port-53 redirect is a dst-nat on the BNG, before the
  CGNAT hop. Unbound should therefore see the subscriber's private IP.
  - Verify with `unbound-control dump_requestlist`, or one `log-queries` line.
  - If it shows a CGNAT/BNG address, the redirect is masquerading, and Method C
    can't attribute queries.
- **dnstap:** Debian/Ubuntu `unbound` packages are normally built with
  `--enable-dnstap`, but confirm from the configure line.
- **Volume:** total queries per day is needed to size the consumer. Many
  customer routers cache DNS, which also affects the beacon-counting feature
  in Method C.

## 6. Datastore

- **This repo:** flow data goes to ClickHouse `radius_telemetry.flow_records`,
  with 14-day TTL and `ORDER BY (subscriber_id, flow_ts)`.
- The current schema has no TTL, window, length, flags or ifIndex columns, so
  it cannot carry these features as-is.
- **Atlas:** the brief says SQLite `flow_log`. Write headroom must be measured
  on `.140.9`:
  - `iostat -x 5`
  - WAL size
  - collector CPU/RSS during the 17:00–02:00 peak
- Per the brief: nothing per-flow from this module goes into SQLite.

## 7. Proposal — feature-table schema and retention

Store: ClickHouse if adopted, otherwise MySQL with the same columns. Rollups
are produced in the collector from 5-minute in-memory aggregates. At ~650
subscribers that is ~190k rows/day for the 5-min table, which is trivial for
either store.

```sql
-- 5-min per-subscriber features (A, B, D); rolled up hourly. 90d.
CREATE TABLE sharing_features_5m (
  bucket_ts        DateTime,            -- 5-min, UTC
  username         LowCardinality(String),
  plan_family      LowCardinality(String),
  is_peak          UInt8,               -- 17:00-02:00 Asia/Beirut
  -- A: TTL
  hop_hist_flows   Map(UInt8, UInt32),  -- hops(0..4+) -> flows
  hop_hist_bytes   Map(UInt8, UInt64),
  ttl255_flows     UInt32,
  rewrite_flows    UInt32,              -- bulk TCP at exactly 64/128
  -- B: concurrency
  max_concurrent_flows UInt32,
  distinct_dst_ip  UInt32, distinct_dst_24 UInt32, distinct_asn UInt32,
  heavy_streams    UInt16,
  heavy_streams_by_label Map(LowCardinality(String), UInt16),
  -- D: OS fingerprint
  fp_clusters      Array(UInt32),       -- cluster ids active in bucket
  fp_by_hop        Map(String, UInt32)  -- "cluster:hop" -> flows
) ENGINE = SummingMergeTree ... ORDER BY (username, bucket_ts)
  TTL bucket_ts + INTERVAL 90 DAY;

-- Hourly DNS features (C). 90d.
CREATE TABLE sharing_dns_1h (
  hour_ts DateTime, username LowCardinality(String),
  query_count UInt32, ecosystems Array(LowCardinality(String)),
  beacon_device_est Map(LowCardinality(String), UInt8),
  reseller_hits Map(LowCardinality(String), UInt32),
  cache_suspect UInt8
) ... TTL hour_ts + INTERVAL 90 DAY;

-- Daily scores. 90d.
CREATE TABLE sharing_scores_1d (
  day Date, username LowCardinality(String),
  score_ttl Float32, score_conc Float32, score_dns Float32,
  score_reseller Float32, score_fp Float32, score_total Float32,
  methods_over_threshold UInt8, config_version UInt32
) ... TTL day + INTERVAL 90 DAY;

-- Evidence samples (sample flows, matched reseller queries). 7d.
CREATE TABLE sharing_evidence (
  ts DateTime, username LowCardinality(String),
  method LowCardinality(String), payload String  -- JSON, no full URLs
) ... TTL ts + INTERVAL 7 DAY;
```

Config and workflow tables go in the relational store, not the time-series
store:
- `sharing_settings`: thresholds, weights, flag, shadow mode, versioned
- `sharing_catalogue`: device markers, reseller markers, fingerprints;
  versioned and editable
- `sharing_calibration_set`
- `sharing_allowlist`
- `sharing_case_disposition`: plus audit, linked to the existing NOC incident
  table

| Data | Retention |
|---|---|
| 5m / 1h features, daily scores | 90 days |
| Evidence samples | 7 days |
| Raw DNS | in-memory ring buffer only, never persisted |
| Raw flows | not written by this module |

## RBAC note

This repo already has the roles: `tenant_admin`, `noc_engineer`, and
`tech_support`. `tech_support` is subscriber-scoped and has no aggregate view.
Access to `/api/sharing/*` would be `tenant_admin` and `noc_engineer` only.

## Needed from you to proceed

1. Choose: Atlas source repo access, or build in `Radius`.
2. Results of the on-site checks in §1–§3 and §5: router prints, the pcap, and
   the SYN/TTL test.
3. The datastore numbers from §6.
4. Sign-off on the §7 schema and retention.
