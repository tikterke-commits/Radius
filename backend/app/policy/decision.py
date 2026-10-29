"""The authorize decision as a pure function.

Everything the engine needs to answer "accept or reject, and with which
attributes" is gathered into PolicyInputs by app/policy/authorize_service.py
(the only DB-touching piece), then decided here. Same function serves three
callers:
  - the rlm_rest authorize call (Access-Accept/Reject),
  - reconcile/actuation (what should a *live* session have right now? — the
    concurrency check is skipped there),
  - the admin API's "why is this subscriber getting this speed" preview.

Order of evaluation:
  1. identity: MAC binding
  2. gates (status / expiration / traffic / uptime) — each failed gate is
     resolved by the profile's configured action: block, throttle or
     walled garden
  3. daily policy hard cap
  4. concurrent sessions
  5. rate: base (override or profile) -> monthly FUP -> daily FUP ->
     exhaustion throttle -> time-of-day windows; most restrictive wins
  6. NAS attributes: pool / static IP, PPP group, address list, timeouts,
     NAS-side byte limit, custom attributes
"""

from dataclasses import dataclass, field
from datetime import datetime

from app.policy.decision_cache import AuthorizeDecision
from app.policy.gates import SubscriberSnapshot, evaluate_gates
from app.policy.quota_engine import FupTier, QuotaKind, QuotaPeriod, apply_fup
from app.policy.rate_limit import RateSpec, format_rate_limit, scale_to
from app.policy.time_of_day import TimeWindow, class_address_lists, own_rate_under_windows, window_active

# Attributes that change continuously (remaining-byte limits) and only make
# sense at session start. Reconcile ignores them when diffing, otherwise
# every interim update would look like a policy change.
VOLATILE_ATTRIBUTES = frozenset({
    "Mikrotik-Total-Limit", "Mikrotik-Total-Limit-Gigawords",
    "Mikrotik-Recv-Limit", "Mikrotik-Recv-Limit-Gigawords",
    "Mikrotik-Xmit-Limit", "Mikrotik-Xmit-Limit-Gigawords",
})

# Attributes the engine computes itself; custom attributes may not set them
# (enforced at the admin API, and core values win here regardless).
RESERVED_ATTRIBUTES = frozenset({
    "Mikrotik-Rate-Limit", "Mikrotik-Address-List", "Framed-IP-Address", "Framed-Pool",
    "Mikrotik-Group", "Session-Timeout", "Idle-Timeout", "Acct-Interim-Interval",
    "User-Name", "User-Password",
}) | VOLATILE_ATTRIBUTES


@dataclass(frozen=True)
class PlanSpec:
    rate: RateSpec
    fup_tiers: tuple[FupTier, ...] = ()
    on_quota_exhausted: str = "block"
    exhausted_rx_bps: int | None = None
    exhausted_tx_bps: int | None = None
    on_expired: str = "block"
    ip_pool: str | None = None
    mikrotik_group: str | None = None
    address_list: str | None = None
    session_timeout_s: int | None = None
    idle_timeout_s: int | None = None
    interim_interval_s: int | None = None
    enforce_quota_on_nas: bool = False
    custom_attributes: tuple[dict, ...] = ()
    default_concurrent_sessions: int = 1


@dataclass(frozen=True)
class FreeWindow:
    start: object  # datetime.time
    end: object
    days: frozenset[str]


@dataclass(frozen=True)
class DailyPolicySpec:
    counted: str = "rxtx"  # rxtx | tx | rx
    tiers: tuple[FupTier, ...] = ()
    daily_quota_bytes: int | None = None
    on_exhaust: str = "throttle"
    exhausted_rx_bps: int | None = None
    exhausted_tx_bps: int | None = None
    free_windows: tuple[FreeWindow, ...] = ()

    def counted_bytes(self, rx: int, tx: int) -> int:
        return {"rx": rx, "tx": tx}.get(self.counted, rx + tx)

    def is_free_time(self, local_now: datetime) -> bool:
        return any(window_active(w.start, w.end, w.days, local_now) for w in self.free_windows)


@dataclass(frozen=True)
class SubscriberSpec:
    enabled: bool = True
    mac_binding: str | None = None
    static_ip: str | None = None
    rx_limit_override_bps: int | None = None
    tx_limit_override_bps: int | None = None
    custom_attributes: tuple[dict, ...] = ()
    exempt_time_of_day: bool = False
    exempt_fup: bool = False
    concurrent_session_limit: int | None = None


@dataclass(frozen=True)
class UsageSnapshot:
    today_counted_rx: int = 0
    today_counted_tx: int = 0
    period_counted_rx: int = 0
    period_counted_tx: int = 0


@dataclass(frozen=True)
class PolicyInputs:
    subscriber: SubscriberSpec
    plan: PlanSpec
    now: datetime  # UTC
    local_now: datetime  # settings.timezone
    expiration: datetime | None = None
    daily: DailyPolicySpec | None = None
    tod_windows: tuple[TimeWindow, ...] = ()
    quota_remaining: dict = field(default_factory=dict)  # {(QuotaKind, QuotaPeriod): int}
    usage: UsageSnapshot = UsageSnapshot()
    calling_station_id: str | None = None
    other_live_sessions: int | None = None  # None = don't check concurrency (reconcile path)
    walled_garden_list: str = "walled-garden"
    walled_garden_rx_bps: int = 256_000
    walled_garden_tx_bps: int = 256_000


def _reject(reason: str, explain: list[str]) -> AuthorizeDecision:
    return AuthorizeDecision(accepted=False, reply_attributes={}, reason=reason, explain=explain)


def _tightest(quota: dict, kind: QuotaKind) -> int | None:
    """Whichever period (daily or monthly) is more restrictive right now — a
    subscriber who's exhausted today's daily cap is blocked even with monthly
    quota untouched."""
    candidates = [quota.get((kind, p)) for p in QuotaPeriod]
    candidates = [v for v in candidates if v is not None]
    return min(candidates) if candidates else None


def decide(inp: PolicyInputs) -> AuthorizeDecision:
    sub, plan = inp.subscriber, inp.plan
    explain: list[str] = []

    # 1. identity
    if sub.mac_binding and inp.calling_station_id and sub.mac_binding.lower() != inp.calling_station_id.lower():
        return _reject("mac_binding_mismatch", [f"bound to {sub.mac_binding}, came from {inp.calling_station_id}"])

    # 2. gates
    gates = evaluate_gates(SubscriberSnapshot(
        enabled=sub.enabled,
        expiration=inp.expiration,
        quota_rx_remaining=_tightest(inp.quota_remaining, QuotaKind.RX),
        quota_tx_remaining=_tightest(inp.quota_remaining, QuotaKind.TX),
        quota_rxtx_remaining=_tightest(inp.quota_remaining, QuotaKind.RXTX),
        quota_uptime_remaining_s=_tightest(inp.quota_remaining, QuotaKind.UPTIME),
        now=inp.now,
    ))

    blocking: list[str] = []
    walled: list[str] = []
    throttle_candidates: list[tuple[int, int, str]] = []

    if not gates.status_gate:
        blocking.append("status")
    if not gates.traffic_gate:
        if plan.on_quota_exhausted == "throttle":
            throttle_candidates.append((
                plan.exhausted_rx_bps or inp.walled_garden_rx_bps,
                plan.exhausted_tx_bps or inp.walled_garden_tx_bps,
                "quota exhausted -> throttled",
            ))
        elif plan.on_quota_exhausted == "walled_garden":
            walled.append("traffic")
        else:
            blocking.append("traffic")
    if not gates.expiration_gate:
        if plan.on_expired == "walled_garden":
            walled.append("expiration")
        else:
            blocking.append("expiration")
    if not gates.uptime_gate:
        blocking.append("uptime")

    # Keep the gate order evaluate_gates reports, so reasons stay stable.
    ordered_blocking = [g for g in gates.failed_gates() if g in blocking]
    if ordered_blocking:
        return _reject(f"gate:{','.join(ordered_blocking)}", [f"{g} gate failed" for g in ordered_blocking])

    # 3. daily policy hard cap
    daily = inp.daily
    today_counted = 0
    if daily is not None:
        today_counted = daily.counted_bytes(inp.usage.today_counted_rx, inp.usage.today_counted_tx)
        if daily.daily_quota_bytes is not None and today_counted >= daily.daily_quota_bytes:
            if daily.on_exhaust == "block":
                return _reject("daily_quota_exhausted", [f"used {today_counted} of {daily.daily_quota_bytes} today"])
            if daily.on_exhaust == "walled_garden":
                walled.append("daily_quota")
            else:
                throttle_candidates.append((
                    daily.exhausted_rx_bps or inp.walled_garden_rx_bps,
                    daily.exhausted_tx_bps or inp.walled_garden_tx_bps,
                    "daily quota exhausted -> throttled",
                ))

    # 4. concurrency
    if inp.other_live_sessions is not None:
        limit = sub.concurrent_session_limit or plan.default_concurrent_sessions or 1
        if inp.other_live_sessions >= limit:
            return _reject("concurrent_limit", [f"{inp.other_live_sessions} live session(s), limit {limit}"])

    reply: dict = {}
    for attr in (*plan.custom_attributes, *sub.custom_attributes):
        name = attr.get("attribute")
        if name and name not in RESERVED_ATTRIBUTES:
            reply[name] = attr.get("value")

    # 5. rate
    if walled:
        spec = RateSpec(rx=inp.walled_garden_rx_bps, tx=inp.walled_garden_tx_bps)
        reply["Mikrotik-Rate-Limit"] = format_rate_limit(spec)
        reply["Mikrotik-Address-List"] = inp.walled_garden_list
        explain.append(f"walled garden ({', '.join(walled)})")
        reason = f"ok:walled_garden:{','.join(walled)}"
    else:
        base_rx = sub.rx_limit_override_bps or plan.rate.rx
        base_tx = sub.tx_limit_override_bps or plan.rate.tx
        if sub.rx_limit_override_bps or sub.tx_limit_override_bps:
            explain.append("subscriber rate override")
        candidates: list[tuple[int, int, str]] = [(base_rx, base_tx, "base rate")]

        if not sub.exempt_fup:
            consumed_period = inp.usage.period_counted_rx + inp.usage.period_counted_tx
            m_rx, m_tx = apply_fup(base_rx, base_tx, list(plan.fup_tiers), consumed_period)
            if (m_rx, m_tx) != (base_rx, base_tx):
                candidates.append((m_rx, m_tx, "monthly FUP tier"))
            if daily is not None and daily.tiers:
                d_rx, d_tx = apply_fup(base_rx, base_tx, list(daily.tiers), today_counted)
                if (d_rx, d_tx) != (base_rx, base_tx):
                    candidates.append((d_rx, d_tx, "daily FUP tier"))
        candidates.extend(throttle_candidates)

        eff_rx = min(c[0] for c in candidates)
        eff_tx = min(c[1] for c in candidates)
        explain.extend(c[2] for c in candidates[1:] if c[0] == eff_rx or c[1] == eff_tx)

        address_list = plan.address_list
        if not sub.exempt_time_of_day and inp.tod_windows:
            eff_rx, eff_tx, notes = own_rate_under_windows(eff_rx, eff_tx, list(inp.tod_windows), inp.local_now)
            explain.extend(notes)
            class_lists = class_address_lists(list(inp.tod_windows), inp.local_now)
            if class_lists:
                # RouterOS takes one list per Mikrotik-Address-List; an active
                # peak list wins over the profile's static list.
                address_list = class_lists[0]
                explain.append(f"tagged {class_lists[0]}")

        spec = scale_to(plan.rate, eff_rx, eff_tx)
        reply["Mikrotik-Rate-Limit"] = format_rate_limit(spec)
        if address_list:
            reply["Mikrotik-Address-List"] = address_list
        throttled = any(c[2].endswith("throttled") for c in throttle_candidates)
        reason = "ok:throttled" if throttled else "ok"

    # 6. NAS attributes
    if sub.static_ip:
        reply["Framed-IP-Address"] = str(sub.static_ip)
    elif plan.ip_pool:
        reply["Framed-Pool"] = plan.ip_pool
    if plan.mikrotik_group:
        reply["Mikrotik-Group"] = plan.mikrotik_group
    if plan.session_timeout_s:
        reply["Session-Timeout"] = int(plan.session_timeout_s)
    if plan.idle_timeout_s:
        reply["Idle-Timeout"] = int(plan.idle_timeout_s)
    if plan.interim_interval_s:
        reply["Acct-Interim-Interval"] = int(plan.interim_interval_s)

    if plan.enforce_quota_on_nas and not walled and reason == "ok":
        remaining = _tightest(inp.quota_remaining, QuotaKind.RXTX)
        if remaining is not None and remaining > 0:
            reply["Mikrotik-Total-Limit"] = remaining & 0xFFFFFFFF
            reply["Mikrotik-Total-Limit-Gigawords"] = remaining >> 32

    if not explain:
        explain.append("base rate")
    return AuthorizeDecision(accepted=True, reply_attributes=reply, reason=reason, explain=explain)


def stable_attributes(reply: dict | None) -> dict:
    """The part of a reply that reconcile compares — volatile byte limits removed."""
    return {k: v for k, v in (reply or {}).items() if k not in VOLATILE_ATTRIBUTES}
