# FreeRADIUS section for Atlas — engine design, deployment, cutover

## What this is

A second, fully independent subscriber universe inside Atlas, next to the
SAS4 one. The engine in this repo owns those subscribers end to end —
FreeRADIUS, policy, quotas, sessions, CoA — and Atlas drives it through the
admin API (`/api/v1`). SAS4 is not involved: no proxying, no shared NAS, no
shared database.

```
                      ┌──────────────────── engine host (docker compose) ───────────────────┐
 MikroTik PPPoE ──1812/1813──▶ freeradius ──rlm_rest──▶ backend ──▶ Postgres (state)        │
 (engine-only)   ◀──CoA 3799── worker ◀── reconcile queue ──┘   └──▶ Redis (cache, queue)     │
                      │                                         └──▶ ClickHouse (acct history)│
 Atlas ──https /api/v1 (X-API-Key, X-Actor)──▶ caddy ──▶ backend                               │
                      └──────────────────────────────────────────────────────────────────────┘
```

Why a separate service rather than code inside Atlas: subscriber logins must
keep working when Atlas is down or redeploying. Atlas going down takes the
NOC screens with it, not authentication.

## What the engine does

| Area | Behavior |
|---|---|
| **Profiles** | Rate (rx/tx), burst rate / threshold / time, priority, limit-at → one `Mikrotik-Rate-Limit`. IP pool or static IP, PPP profile (`Mikrotik-Group`), address list, session / idle timeout, interim interval, NAS-side byte limit, custom attributes. Price + validity for renewals. |
| **Quotas** | rx / tx / rx+tx / uptime, monthly and daily. Monthly resets at renewal or on the calendar month. Exhausted → block, throttle to a set rate, or walled garden. |
| **Daily policies** | SAS4-style: daily FUP tiers on today's usage, optional daily cap with its own exhaust action, free windows (recorded, not counted). Download-only / upload-only / both counting. |
| **Time policies** | Windows by weekday + local time (wraps midnight). `applies_to=all` scales or caps the subscriber's own rate — the "simple queue drops to 25% at peak" model, pushed by CoA at the window edge. A traffic-class window instead tags an address list for router-side shaping. |
| **Expiry** | Block or walled garden. Renew extends from the later of now / current expiry. Scheduled profile changes (at a time or next renewal). |
| **Sessions** | Concurrency limit (a redial from the same CPE MAC isn't counted as a second session), MAC binding, stale-session sweep, 64-bit counters via gigawords. |
| **Actuation** | Reconcile, not delta: diff what a live session should have against what it was given. CoA for mutable attributes, Disconnect for everything else; CoA-NAK falls back to Disconnect. Dry-run by default, per-minute ceiling, panic switch, every action audited with the NAS reply. |
| **Visibility** | Auth log with the engine's reject reason, decision "explain" trail, per-day usage, session history, audit log, stats. |

All policy windows use `TIMEZONE` (Asia/Beirut). The authorize path in the
earlier commits evaluated windows against UTC — a 17:00 peak started at 19:00
or 20:00 local depending on DST. Fixed and tested.

## Deploy

1. On the engine host: `cp .env.example .env`, fill every secret, set
   `ENGINE_API_SITE` to the address Atlas will dial. Leave
   `ACTUATION_DRY_RUN=true`.
2. `docker compose up -d --build`. The backend runs migrations before it
   takes traffic; the worker starts after the backend is healthy.
3. `docker compose exec caddy cat /data/caddy/pki/authorities/local/root.crt`
   → copy to the Atlas host; Atlas's client trusts it via `verify=<path>`.
4. Firewall: 1812-1813/udp from engine PPPoE servers only; 8443/tcp from the
   Atlas host only. Port 8000 stays on loopback.

## NAS (RouterOS) — a PPPoE server dedicated to engine subscribers

RouterOS asks its `/radius` servers in order and moves to the next only on a
**timeout**, never on a reject. A PPPoE server pointed at both SAS4 and the
engine would therefore reject every SAS4 user the engine doesn't know. Engine
subscribers need their own PPPoE server instance — a CHR, or a separate
VLAN / interface / service-name on a box whose `/radius` lists only the engine.

```routeros
/radius add address=<ENGINE_HOST_IP> secret="<same secret as in Atlas NAS form>" service=ppp
/radius incoming set accept=yes port=3799
/ppp aaa set use-radius=yes accounting=yes interim-update=5m
```

Then register the box in Atlas → FreeRADIUS → NAS (same IP, same secret).
The engine renders the FreeRADIUS client entry and radiusd restarts itself.

CoA is accepted only from an address in `/radius`, so the worker must send
from `<ENGINE_HOST_IP>` — true when it runs on the same docker host as
freeradius (default compose).

## Cutover checklist

- [ ] Test profile + test subscriber created from Atlas; PPPoE login on the
      engine PPPoE server; auth log shows `accept / ok`.
- [ ] `Mikrotik-Rate-Limit` on the router's dynamic queue matches the
      profile's preview string.
- [ ] Interim updates arriving (session row updates; usage chart moves).
- [ ] Dry-run plans in the audit log look right for a day (`actuation.*`
      rows with `nas_response.dry_run=true`).
- [ ] With one test account: flip `ACTUATION_DRY_RUN=false`, restart
      backend + worker, then:
  - [ ] change the profile rate → CoA-ACK, queue changes without redial
        (ROS7 **and** ROS6 6.49.19; if ROS6 ignores it, set
        `capability_matrix.coa_rate_change_applies_midsession=false` on that
        NAS and the engine redials instead)
  - [ ] disable → Disconnect-ACK, login then rejected `gate:status`
  - [ ] peak window edge → CoA at the minute
- [ ] Only then move real subscribers onto the engine PPPoE server.

## Not built yet (deliberately)

- **Billing flows** — the engine has balance / invoice / voucher tables but no
  admin endpoints for them. Renew extends service; charging, manager credit
  and recharge cards still need endpoints (and Whish/WhatsApp recharge
  wiring for engine subscribers).
- **SMS notifications** (expiry alerts) — not in this slice.
- **Postgres RLS** — tenant scoping is enforced in application code; the
  RLS policies described in `00-part-h-design.md` aren't created yet.
- **LibreQoS export** — the hybrid design in `00-part-h-design.md §3` stands;
  no feed from the plan catalog to LibreQoS yet.
- **Mikrotik-Address-List via CoA** is off by default (not verified on your
  ROS builds); with it off, address-list changes redial the session.
