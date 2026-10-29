# Claude Code prompt — add the FreeRADIUS section to Atlas

> Run this in Claude Code **on the Atlas host (10.140.140.9), in `/opt/dpc`**.
> Atlas already exists and is in production. Extend it; do not scaffold a new
> project, do not restructure what's there.

---

## Context

Atlas is Data Plus Lebanon's ISP automation platform and SAS4 wrapper:
FastAPI backend (`backend/app/main.py`, systemd `dpc.service`, deployed at
`/opt/dpc`), MikroTik fleet, SNMP subscriber monitoring
(`backend/app/snmp/client.py`), SAS4 client (`backend/app/sas4/client.py`),
existing NOC UI and RBAC (including a technical-support role).

A separate service — the **RADIUS engine** (repo `tikterke-commits/radius`) —
now owns a second, independent set of subscribers: FreeRADIUS + policy engine
+ quotas + CoA. It exposes an admin API at `https://<engine>:8443/api/v1`
(API key auth). **Atlas must get a new "FreeRADIUS" section that drives it**,
alongside — and completely separate from — the existing SAS4 section.

You have three artifacts from the engine repo; copy them into this repo first:

1. `clients/atlas/radius_engine.py` → `backend/app/integrations/radius_engine.py`
   (typed async client for every endpoint; contract-tested against the engine)
2. `docs/openapi-admin.json` → keep alongside as the API reference
3. `docs/01-atlas-freeradius-section.md` → engine behavior, units, NAS setup

## Hard rules

- **Do not touch the SAS4 section** — its routes, pages, client, data, or
  jobs. Engine subscribers and SAS4 subscribers never mix in one list.
- **The engine is the source of truth** for its subscribers. Atlas stores
  none of their data (no SQLite/MySQL tables, no caches beyond request
  scope). Every read and write goes through `RadiusEngine`.
- **Never send the engine API key to the browser.** The UI calls Atlas
  routes; Atlas routes call the engine server-side.
- **Always pass the logged-in Atlas username as `actor`** — the engine's
  audit log records it.
- Map engine errors through: `EngineError.status` 404/409/422 → same status
  with `detail`; anything else → 502 "engine unavailable".
- Units: the API speaks **bps and bytes**. The UI shows **Mbps and GB**
  (decimal: 1 Mbps = 1,000,000 bps, 1 GB = 1,000,000,000 bytes). Convert only
  at the UI edge.

## Step 0 — Recon (report before writing code)

Read and summarize, briefly:
- how routers are registered in `backend/app/main.py` and where page routes /
  templates / frontend views live
- how config/secrets are loaded (env? `config.yaml`?)
- how RBAC is enforced (decorator? dependency?) and the existing roles
- how the SAS4 subscriber list and detail pages are built, so the new pages
  reuse the same layout components and styling
- how the SNMP live-traffic widget is fed (it's keyed by PPPoE ifName =
  username — that keeps working for engine subscribers on any polled NAS)

## Step 1 — Wiring

- Config keys (in Atlas's existing config pattern): `RADIUS_ENGINE_URL`,
  `RADIUS_ENGINE_API_KEY`, `RADIUS_ENGINE_CA_BUNDLE` (path to Caddy's
  `root.crt`). Section hidden entirely if `RADIUS_ENGINE_URL` is unset.
- One shared `RadiusEngine` instance created at startup, closed at shutdown.
- Backend router `backend/app/api/freeradius.py`, prefix `/api/freeradius`,
  thin pass-through to the client — one Atlas route per engine route.

## Step 2 — RBAC

| Role | Allowed |
|---|---|
| Admin | everything, including NAS, policies, panic switch, delete |
| NOC engineer | everything except NAS secrets and delete |
| Technical support | read subscribers / sessions / usage / auth log; actions: disconnect, MAC bind/clear, reconcile. **No** profile/policy/NAS edits, no renew, no delete. No aggregate stats beyond counts. |
| Billing | read subscribers; renew, top-up, change profile |

Reuse Atlas's existing role mechanism; add permissions, don't invent a new
system. Enforce in the backend router, and hide disallowed controls in the UI.

## Step 3 — Pages (match the current Atlas shell exactly)

Navigation: a **FreeRADIUS** group, visually distinct from **SAS4**.

1. **Overview** — `stats()`: total / active / expired / disabled / online,
   expiring in 7 days, per-profile counts, today's traffic, rejects in the
   last hour. A permanent **DRY-RUN** banner while `actuation.dry_run` is
   true, and a **PANIC: actuation halted** banner when `panic` is on.
2. **Subscribers** — `list_subscribers()` with search, status / online /
   profile / expiring filters, sort, pagination, row checkboxes → bulk
   enable / disable / renew / change profile / disconnect.
3. **Subscriber detail** — `get_subscriber()` returns everything for the
   header and overview:
   - status chip, online dot, profile, expiry + days left, scheduled change
   - **four gate chips** (status / traffic / expiration / uptime) from `gates`
   - **current decision**: `decision.reason`, `decision.explain` as a list,
     and the live `Mikrotik-Rate-Limit` rendered as "↓ X Mbps / ↑ Y Mbps"
     (rx = upload, tx = download)
   - quotas as bars (remaining / allowance, reset time)
   - today and period usage
   - tabs: **Live traffic** (existing SNMP widget by username), **Usage**
     (`usage(days=30)` stacked rx/tx; counted vs raw), **Sessions**
     (`session_history`), **Auth log**, **Audit** (`audit(subject=
     "subscriber:<id>")`), **Edit** (contact, static IP, MAC, concurrency,
     rate override, custom attributes, exemptions)
   - actions: renew (days, reset quota?), change profile (now / next renewal
     / at date), top-up (GB), reset quota (daily / monthly / all), set or
     generate password (show a generated password once, never store it),
     MAC bind-current / set / clear, disconnect, push policy now (reconcile),
     enable / disable, delete (admin; typed-username confirmation)
4. **New subscriber** — username, password or generate, profile, expiry or
   validity days, contact/KYC fields, static IP, MAC.
5. **Profiles** — list with subscriber counts and `rate_limit_preview`.
   Editor sections: **Speed & QoS** (download/upload, burst, threshold, burst
   time, priority 1–8, limit-at — live preview of the resulting
   `Mikrotik-Rate-Limit` string as fields change), **Quotas** (monthly/daily
   × rx/tx/total/uptime, monthly reset mode, exhausted action + throttle
   speed), **FUP tiers** (after GB → speed rows), **Policies** (daily policy,
   time policy pickers), **NAS attributes** (IP pool, PPP profile, address
   list, session/idle timeout, interim interval, NAS-side quota), **Custom
   attributes** (attribute/value rows; show the engine's 422 inline),
   **Expiry** action, price, validity days. A **Simulate** panel: pick a
   date-time and usage, call `simulate_profile`, show the reply attributes and
   explain trail.
6. **Daily policies** — counted traffic (both/download/upload), FUP tiers,
   daily cap + exhaust action, free windows on a 24h × weekday grid.
7. **Time policies** — windows on a 24h × weekday grid; per window:
   applies to all traffic (multiplier or absolute Mbps) or a traffic class
   (address list). Show which windows are `active_now`.
8. **NAS** — name, IP, secret (write-only; blank on edit means unchanged),
   ROS version, CoA port, enabled, capability flags
   (`coa_rate_change_applies_midsession`). Show online session counts and the
   `clients_file` result returned by create/update (written? error?). Show
   the RouterOS snippet from `01-atlas-freeradius-section.md` with the
   engine IP filled in.
9. **Online sessions** — `online_sessions()` with NAS / search filters,
   per-row disconnect.
10. **Auth log** — recent rejects first, filter by username/result.
11. **Audit** — filter by subject / action / actor.
12. **Actuation** (admin) — dry-run status (read-only; it's an engine env
    setting), pending queue size, CoA-mutable attributes, **panic switch**
    with confirmation.

## Step 4 — Tests

- Backend route tests with the engine mocked (`respx`): pass-through,
  actor header, error mapping, RBAC per role (especially tech-support
  denials).
- One smoke test that the section is hidden when `RADIUS_ENGINE_URL` is unset.
- Existing Atlas tests must still pass.

## Step 5 — Deliver

- Short summary of files added/changed.
- Exact config lines to add on 10.140.140.9 and the restart command for
  `dpc.service`.
- Do not restart production yourself; print the commands.
