# Part H — Design Decisions Before Writing Code

## 0. Two constraints this session actually has

Stated up front because they change what "port from Atlas" can mean here:

- **No access to the Atlas source tree.** This session has no filesystem or network path to `/opt/dpc` on `10.140.140.9`, and this repository (`tikterke-commits/radius`) was empty at the start of this work — no Atlas code was ever copied in. Everything below that references Atlas is reasoned from the *operational history and behavioral description* in the build prompt (SNMP-via-ifName, hash-chained ledger, SQLite failure modes, NetFlow drop-in, RouterOS SSH doctrine), not from reading its code. "Port" therefore means *reimplement the proven technique*, not *copy the file*. If the actual Atlas source becomes reachable later (e.g. pasted in, or this session gets a network path to `.140.9`), the SNMP collector and RouterOS actuation layer in particular are worth a real diff against what's built here.
- **No network route to `10.140.140.9` or any internal host.** Confirmed by a direct connection test — blocked, as expected for a sandboxed cloud session. Nothing in this build can SSH into the BNG fleet, hit FreeRADIUS on the production box, or `docker compose up` anything on `.140.9`. What this session produces is code, migrations, and compose files in this repo; deploying them to the real host is a step you run yourselves. I will not claim to have deployed anything.

Given that, this is a **greenfield build**, informed by your operational history rather than a port in the literal sense. That matches what Part C already says about several subsystems, just extended to all of them by necessity.

## 1. Where I'd disagree with the prompt, or push back on scope

You asked for this, so plainly:

**This is scoped as a 6-12 month platform for a 650-subscriber ISP, in one build prompt.** FreeRADIUS core + four-gate policy engine + quota engine + billing + multi-tenant RBAC + SNMP telemetry + NetFlow/ASN intelligence + a lawful-intercept hash-chained ledger + RouterOS SSH actuation with generated config diffing + financial/compliance reporting + a three-tier IDS + a subscriber portal + a NOC admin UI + LibreQoS integration + a legacy import tool with AES-wrapped pagination quirks + shadow-mode cutover — that is nine or ten separately shippable products bundled as "v1." I'm going to build this as a sequence of independently deployable slices, and I'd recommend treating everything past the RADIUS/billing/portal core as v1.1+ rather than a merged v1, specifically:

- **True v1 (blocks cutover):** FreeRADIUS + four-gate authz + quota engine + credential remediation + core billing (balance/loan/invoices/vouchers) + RBAC + NAS actuation for rate/CoA + import + shadow mode + basic admin UI + basic subscriber portal. This alone is what makes SAS4 replaceable.
- **v1.1 (valuable, not cutover-blocking):** SNMP telemetry, reports, LibreQoS integration, notifications.
- **v1.2 (genuinely separate program):** the lawful CGNAT ledger and the three-tier IDS. Both are real obligations, but neither blocks getting off SAS4, and both deserve their own review cycle (the ledger has legal retention implications; the IDS has false-positive/notification-fatigue implications) rather than being squeezed into the same PR series as quota math.

I'm not dropping anything from scope — I'm sequencing it so the schema has the right hooks from day one (so v1.2 isn't a redesign) without blocking the actual cutover on IDS detector tuning.

**Multi-tenancy: row-scoped isolation, not schema-per-tenant.** The prompt says "proper tenant isolation," which could mean schema-per-tenant or database-per-tenant. For a business that's reselling to other small ISPs (not hosting thousands of large tenants), that's the wrong axis of isolation — it multiplies migration and operational burden for no real gain at this scale. I'm using a single schema, `tenant_id` on every row, Postgres Row-Level-Security policies enforcing tenant scope at the database layer (not just in application code), and the same mechanism serves the A7 technician row-scoping requirement for free — a technician role is just an RLS policy scoped to `tenant_id = :caller_tenant`. If a future reseller needs hard isolation (their own backup schedule, their own compliance boundary), that's a database-per-tenant *deployment* decision made per-tenant, not a day-one schema decision.

**ClickHouse is right, but for write-pattern reasons, not scale reasons — see §3.** 650 subscribers is not "big data." The reason SQLite falls over at 50M rows/day isn't row count, it's SQLite's single-writer lock under concurrent append load from many pollers/collectors at once. A single-node ClickHouse instance on the DL380 is not a meaningfully bigger operational burden than Postgres — one binary, no cluster needed at this scale — and it's built for exactly this write shape. I'd resist the temptation to read "ClickHouse" as "we now run a big-data platform"; it's one more container in the compose file.

**MS-CHAPv2/CHAP: decide against, not "document the tradeoff and move on."** See §5 — I'm making this call explicitly rather than leaving it open, because "PAP-only vs NT-hash" is exactly the kind of decision that drifts into "well let's just support both" if left ambiguous, which is how the plaintext situation this migration is trying to fix happened in the first place.

## 2. Storage tiering and the ClickHouse boundary

Three tiers, split by write pattern and query shape, not by "how important is this data":

| Store | What lives there | Why |
|---|---|---|
| **Postgres** | Tenants, subscribers, credentials, plans/profiles, quota *state* (current counters + reset markers), billing (balance, loan_balance, invoices, payments, vouchers), RBAC, audit log, NAS inventory, `radcheck`/`radreply`/`radgroupcheck`/`radgroupreply`/`nas` (FreeRADIUS SQL fallback tables) | Low write rate, needs transactions, foreign keys, RLS, and point lookups by primary key — exactly what an RDBMS is for. |
| **ClickHouse** | `radacct` history (append-only accounting), SNMP telemetry samples, NetFlow/IPFIX records, the CGNAT lawful ledger, security-detector observations, rollups for reports | High-volume, append-only, time-ordered, queried in aggregate (sum bytes by subscriber by day, percentile RTT by sector) rather than by single-row lookup. This is the write shape that killed SQLite: many concurrent appenders, no need for row-level locking, no need for transactional consistency across rows. ClickHouse's MergeTree engine is built to absorb exactly this without a global write lock, and its columnar layout makes the aggregate queries (which is nearly everything this data is used for — reports, quota rollups, ASN breakdowns) cheap instead of a full-table scan. |
| **Redis** | Live quota counters (hot path, incremented per accounting interim-update), the authorize decision cache, session presence, OTP codes | Sub-millisecond, ephemeral-tolerant, exactly what the p99<50ms authorize budget needs — Postgres/ClickHouse are not on the authorize hot path at all in the common case. |

**The boundary rule:** if a table is written once-per-billing-event and read by primary key, it's Postgres. If it's written once-per-accounting-packet (i.e. every 30s–5min per active session, times ~650+ sessions, forever) and read in aggregate, it's ClickHouse. `radacct` is the clearest example — FreeRADIUS's own default schema treats it as a relational table, but at this write rate it belongs in the same tier as flow logs, not next to `radcheck`. I'm keeping a thin `radacct`-shaped view for FreeRADIUS SQL-fallback-mode compatibility (Part A1's degraded fallback), but the real accounting history sink is ClickHouse.

**Quota state vs quota history is the one place this split needs care:** the *current remaining balance* for each of the four quota types (rx/tx/rxtx/uptime, monthly and daily) is small, hot, and needs to gate an authorize decision in <50ms — that's Redis-backed with Postgres as the durable source of truth on reset boundaries, not ClickHouse. The *ledger of how it got consumed* (every accounting interim update that decremented it) is ClickHouse. Same event, two destinations, different reasons.

**Retention is per data class, not global:**
- Lawful CGNAT ledger: driven by Lebanese telecom data-retention obligations, not convenience — needs a real answer from you/legal before I pick a number here. I'm defaulting the schema to indefinite retention with a documented override point, not silently rotating it out.
- NetFlow/IPFIX raw: short (7-14 days) — it's for live ASN/destination intelligence and incident investigation, not long-term analytics.
- SNMP telemetry: rollup after 30-90 days (keep daily/hourly aggregates, drop raw samples).
- `radacct` history: rollup after 90 days into daily-per-subscriber usage aggregates for reporting; raw kept ~1 year for billing dispute resolution.

These are proposed defaults in the migration/retention config, reviewable, not hardcoded assumptions about your legal obligations.

## 3. LibreQoS vs `Mikrotik-Rate-Limit` — the open question

**Recommendation: hybrid. RADIUS keeps setting `Mikrotik-Rate-Limit` as a fail-safe entitlement ceiling; LibreQoS is the primary shaper and the source of quality measurement.** Not a split-the-difference hedge — here's the actual argument for each pure position and why neither wins outright.

**Argument for RADIUS-only (no LibreQoS shaping, `Mikrotik-Rate-Limit` does everything):**
Simple, already understood by the ops team, no new inline failure point, no bypass-NIC sizing problem. But this is *the mechanism already in production today*, and it's exactly what produces the "is it them or is it us" ambiguity the prompt wants solved. MikroTik's simple-queue/PCQ shaping is not topology-aware, doesn't run CAKE, and doesn't passively measure RTT/retransmits per subscriber. Keeping it as the only shaper means deploying LibreQoS bought you nothing but a second dashboard.

**Argument for LibreQoS-only (RADIUS stops setting `Mikrotik-Rate-Limit` entirely, rate lives only in LibreQoS):**
Single shaping authority, best queue quality (CAKE/fq_codel), no risk of two enforcers fighting over the same flow with different numbers after a plan change. But LibreQoS is inline and therefore in the failure path by the prompt's own framing. If it's down, bypassed (even briefly, during a bypass-NIC failover event), or a subscriber's session isn't yet correctly mapped into its topology (new install, address change, config lag), there is *no rate limit at all* — that's not a QoE degradation, it's a plan-integrity and billing-fairness problem: a capped-plan subscriber gets uncapped full-uplink speed with no gate. `Mikrotik-Rate-Limit` is also independently required by Part E for the FUP-tier and peak-window `Mikrotik-Address-List` steering mechanics — those are MikroTik-native and don't route through LibreQoS at all, so RADIUS is setting NAS attributes at authorize time regardless of the shaping decision.

**Why hybrid and not a compromise-for-its-own-sake:** the two mechanisms aren't actually doing the same job. `Mikrotik-Rate-Limit` becomes a coarse, always-on, NAS-native ceiling set at the subscriber's plan speed — cheap to enforce, fails safe if the inline box is bypassed, and it's the same attribute already carrying the address-list/walled-garden mechanics. LibreQoS is fed the same plan-speed truth via its own config pipeline and owns the *shaping curve* (CAKE queue discipline) and the passive RTT/retransmit measurement — it should be configured at or fractionally above the MikroTik ceiling so its queue is what actually produces the experienced shaping behavior in the common case, with the MikroTik limit rarely the binding constraint. That avoids two active enforcers disagreeing about the number after a plan change (single source of truth: the plan catalog, pushed to both), while keeping a real fallback when the inline path is down.

This still leaves the bypass-NIC/failover sizing question as an infrastructure decision for whoever racks the box — noting it, not solving it here, since it's hardware procurement, not software.

## 4. Schema proposal (high level — full DDL in migrations)

**Postgres — core domain:**
- `tenants` (id, name, parent_tenant_id nullable → reseller hierarchy, replaces legacy `parent_id`)
- `subscribers` (tenant_id, username, firstname, lastname, company, phone, email, address, city, notes, national_id, contract_id, static_ip, mac_binding, concurrent_session_limit, created_at, created_by, legacy_sas4_id nullable — import provenance)
- `credentials` (subscriber_id, password_hash [Argon2id], algo_version, rotated_at — see §5)
- `plans` (tenant_id, name, normalized_tier, rx_limit, tx_limit, uptime_limit_daily/monthly, fup_tiers jsonb, time_of_day_policy_id)
- `time_of_day_policies` (name, windows jsonb — e.g. peak 17:00-24:00, applies_to jsonb — e.g. "international")
- `subscriptions` (subscriber_id, plan_id, started_at, next_profile_change nullable, expiration)
- `quota_state` (subscriber_id, period [monthly/daily], kind [rx/tx/rxtx/uptime], remaining, resets_at) — Postgres durable copy; Redis mirrors the hot path
- `gate_status` (subscriber_id, status_gate, traffic_gate, expiration_gate, uptime_gate, computed_at) — the four-gate model, independently queryable, not a single boolean
- `balances` (subscriber_id, balance, loan_balance) — explicitly separate columns, per A6
- `invoices`, `payments`, `vouchers` (with attempt_count/attempt_limit), `addons`, `addon_subscriptions`
- `nas_devices` (ip, secret, ros_version, capability_matrix jsonb — per Part E)
- `roles`, `role_assignments` (tenant-scoped, RLS-enforced)
- `audit_log` (actor, action, subject, old_value, new_value, nas_response nullable — covers Part E's "every speed change is audited")
- `radcheck`/`radreply`/`radgroupcheck`/`radgroupreply`/`nas` — FreeRADIUS-standard shape, fallback mode only

**ClickHouse — high-volume/append-only:**
- `radacct_events` (accounting history, MergeTree partitioned by day)
- `snmp_samples` (subscriber_username, ifname, nas_ip, rx_bytes, tx_bytes, ts)
- `flow_records` (pre-NAT 5-tuple, asn, bytes, ts)
- `lawful_ledger` (session/NAT-translation entries, hash_prev, hash_self, ts) — hash-chained, append-only fits MergeTree naturally
- `security_observations` (tier, detector, subscriber_id nullable, bps_out, threshold_bps, baseline_bps_out_p95, ts)

**Redis:**
- `quota:{subscriber_id}:{kind}:{period}` counters
- `authz_cache:{username}` decision cache, short TTL
- session presence, OTP codes

I'll write this as actual Alembic migrations + ClickHouse DDL next, matching this proposal; flagging here rather than silently deciding so you can redirect before it's 40 files deep.

## 5. Credential decision (A5) — decided, not left open

**PAP-only. Argon2id storage. MS-CHAPv2/CHAP explicitly disabled in the FreeRADIUS virtual server.**

MS-CHAPv2 needs the NT-hash (unsalted MD4 of the UTF-16 password) available server-side to compute the challenge-response — it is structurally incompatible with a proper salted hash like Argon2id/bcrypt. Storing NT-hashes to keep MS-CHAPv2 available would mean storing a fast, unsalted, decades-broken hash right after this migration's stated purpose is fixing "every credential should be treated as compromised." That's the drift this section of the prompt is warning against, so I'm closing it off rather than leaving both paths theoretically open: no NT-hash column, no MS-CHAPv2 in the RADIUS config.

PAP is defensible here specifically because of your topology, not as a general default: the PPPoE session runs CPE→AP over WPA2-encrypted 802.11, then AP→BNG over infrastructure you own end-to-end — there's no untrusted hop the password crosses in the clear before it hits your own RADIUS server, and RADIUS's own PAP encoding (RFC 2865 `User-Password`, shared-secret-XOR'd) is already what protects it on the wire between NAS and RADIUS regardless. If a future hotspot/EAP flow needs a different auth method, that's a separate, explicitly-scoped credential class for those accounts only — not a blanket re-opening of NT-hash storage for all 650 PPPoE subscribers.

Rotation: staged, gated behind explicit operator confirmation per Part A5 — building the mechanism (forced-rotation flag + SMS notification + grace window) now, not auto-triggering it against production from this session.

## 6. What's actually next

Proceeding to scaffold the repo (compose stack, backend skeleton, migrations, FreeRADIUS config, policy engine) per the task list, in separate small commits. This doc is committed first so the reasoning is reviewable before 500 files of code assume it.
