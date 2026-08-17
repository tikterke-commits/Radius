"""Ties the pure policy modules (gates, quota_engine, time_of_day) to real
subscriber/plan/quota state and produces the AuthorizeDecision that the
rlm_rest endpoint returns to FreeRADIUS. This is the one place in the
policy layer that touches the database — everything it calls into is a
pure function, which is what made those pieces unit-testable without a DB.
"""

from datetime import datetime, time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.plan import Plan, Subscription, TimeOfDayPolicy
from app.models.quota import QuotaState
from app.models.subscriber import Subscriber
from app.policy.decision_cache import AuthorizeDecision
from app.policy.gates import SubscriberSnapshot, evaluate_gates
from app.policy.quota_engine import FupTier, QuotaKind, QuotaPeriod, apply_fup
from app.policy.time_of_day import TimeWindow, effective_multiplier

INTERNATIONAL_PEAK_LIST = "international-peak"


async def compute_authorize_decision(
    db: AsyncSession, username: str, calling_station_id: str | None, now: datetime
) -> AuthorizeDecision:
    subscriber = (
        await db.execute(select(Subscriber).where(Subscriber.username == username))
    ).scalar_one_or_none()

    if subscriber is None:
        return AuthorizeDecision(accepted=False, reply_attributes={}, reason="no_such_subscriber")

    if subscriber.mac_binding and calling_station_id and subscriber.mac_binding.lower() != calling_station_id.lower():
        return AuthorizeDecision(accepted=False, reply_attributes={}, reason="mac_binding_mismatch")

    subscription = (
        await db.execute(select(Subscription).where(Subscription.subscriber_id == subscriber.id))
    ).scalar_one_or_none()
    if subscription is None:
        return AuthorizeDecision(accepted=False, reply_attributes={}, reason="no_active_subscription")

    plan = (await db.execute(select(Plan).where(Plan.id == subscription.plan_id))).scalar_one_or_none()
    if plan is None:
        return AuthorizeDecision(accepted=False, reply_attributes={}, reason="plan_not_found")

    quota_rows = (
        await db.execute(select(QuotaState).where(QuotaState.subscriber_id == subscriber.id))
    ).scalars().all()
    quota_by_key = {(row.kind, row.period): row.remaining for row in quota_rows}

    snapshot = SubscriberSnapshot(
        enabled=subscriber.enabled,
        expiration=subscription.expiration,
        quota_rx_remaining=_tightest(quota_by_key, QuotaKind.RX),
        quota_tx_remaining=_tightest(quota_by_key, QuotaKind.TX),
        quota_rxtx_remaining=_tightest(quota_by_key, QuotaKind.RXTX),
        quota_uptime_remaining_s=_tightest(quota_by_key, QuotaKind.UPTIME),
        now=now,
    )
    gates = evaluate_gates(snapshot)
    if not gates.all_pass:
        return AuthorizeDecision(
            accepted=False, reply_attributes={}, reason=f"gate:{','.join(gates.failed_gates())}"
        )

    consumed_rxtx = _consumed_this_month(plan, quota_by_key)
    fup_tiers = [FupTier(**tier) for tier in plan.fup_tiers]
    rx_limit_bps, tx_limit_bps = apply_fup(plan.rx_limit_bps, plan.tx_limit_bps, fup_tiers, consumed_rxtx)

    address_lists = []
    if subscriber.static_ip is None:
        pass  # placeholder: walled-garden/grace-period list membership goes here once billing grace is wired up

    if plan.time_of_day_policy_id is not None:
        policy = (
            await db.execute(
                select(TimeOfDayPolicy).where(TimeOfDayPolicy.id == plan.time_of_day_policy_id)
            )
        ).scalar_one_or_none()
        windows = [_parse_window(w) for w in (policy.windows if policy else [])]
        # The peak multiplier is deliberately NOT applied to the subscriber's
        # overall Mikrotik-Rate-Limit here — that would throttle local traffic
        # along with international, which isn't the policy. Instead, membership
        # in an address-list tags the subscriber for the peak window; the
        # router-side queue-tree/mangle config (RouterOS actuation layer, not
        # built in this commit) applies the reduced cap specifically to
        # traffic classified as international, keyed off list membership —
        # per Part E's "router rules key off list membership instead of
        # per-user rule churn."
        if effective_multiplier(windows, now, "international") < 1.0:
            address_lists.append(INTERNATIONAL_PEAK_LIST)

    reply = {
        "Mikrotik-Rate-Limit": f"{rx_limit_bps}/{tx_limit_bps}",
    }
    if address_lists:
        reply["Mikrotik-Address-List"] = ",".join(address_lists)

    return AuthorizeDecision(accepted=True, reply_attributes=reply, reason="ok")


def _tightest(quota_by_key: dict, kind: QuotaKind) -> int | None:
    """The gate/rate-limiting decision uses whichever period (daily or
    monthly) is more restrictive right now — a subscriber who's exhausted
    today's daily cap is blocked even with monthly quota untouched."""
    monthly = quota_by_key.get((kind, QuotaPeriod.MONTHLY))
    daily = quota_by_key.get((kind, QuotaPeriod.DAILY))
    candidates = [v for v in (monthly, daily) if v is not None]
    if not candidates:
        return None
    return min(candidates)


def _consumed_this_month(plan: Plan, quota_by_key: dict) -> int:
    remaining = quota_by_key.get((QuotaKind.RXTX, QuotaPeriod.MONTHLY))
    if remaining is None or plan.quota_rxtx_monthly is None:
        return 0
    return max(0, plan.quota_rxtx_monthly - remaining)


def _parse_window(raw: dict) -> TimeWindow:
    """TimeOfDayPolicy.windows stores plain JSON (see app/models/plan.py's
    docstring example: {"start": "17:00", "end": "24:00", ...}) — this is the
    one deserialization point between that JSON shape and the TimeWindow
    dataclass time_of_day.py's pure functions actually operate on.
    """
    return TimeWindow(
        start=_parse_hhmm(raw["start"]),
        end=_parse_hhmm(raw["end"]),
        days=frozenset(raw["days"]),
        speed_multiplier=raw["speed_multiplier"],
        applies_to=raw["applies_to"],
    )


def _parse_hhmm(value: str) -> time:
    if value == "24:00":  # JSON convention for end-of-day, time.max isn't expressible as HH:MM
        return time(23, 59, 59, 999999)
    hour, minute = value.split(":")
    return time(int(hour), int(minute))
