"""Loads everything app.policy.decision.decide() needs from Postgres and runs
it. The only DB-touching piece of the policy layer; decide() and everything
under it stay pure functions.
"""

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.plan import DailyPolicy, Plan, Subscription, TimeOfDayPolicy
from app.models.quota import QuotaState
from app.models.session import RadiusSession
from app.models.subscriber import Subscriber
from app.models.usage import UsageDaily
from app.policy.clock import parse_hhmm, policy_day, to_local
from app.policy.decision import (
    DailyPolicySpec,
    FreeWindow,
    PlanSpec,
    PolicyInputs,
    SubscriberSpec,
    UsageSnapshot,
    decide,
)
from app.policy.decision_cache import AuthorizeDecision
from app.policy.quota_engine import FupTier
from app.policy.rate_limit import RateSpec
from app.policy.time_of_day import WEEKDAY_NAMES, parse_window

# Kept for backwards compatibility with callers/tests that imported it here.
INTERNATIONAL_PEAK_LIST = "international-peak"


def plan_spec(plan: Plan) -> PlanSpec:
    return PlanSpec(
        rate=RateSpec(
            rx=plan.rx_limit_bps,
            tx=plan.tx_limit_bps,
            burst_rx=plan.burst_rx_bps,
            burst_tx=plan.burst_tx_bps,
            threshold_rx=plan.burst_threshold_rx_bps,
            threshold_tx=plan.burst_threshold_tx_bps,
            burst_time_s=plan.burst_time_s,
            priority=plan.priority,
            min_rx=plan.min_rx_bps,
            min_tx=plan.min_tx_bps,
        ),
        fup_tiers=tuple(FupTier(**t) for t in (plan.fup_tiers or [])),
        on_quota_exhausted=plan.on_quota_exhausted or "block",
        exhausted_rx_bps=plan.exhausted_rx_bps,
        exhausted_tx_bps=plan.exhausted_tx_bps,
        on_expired=plan.on_expired or "block",
        ip_pool=plan.ip_pool,
        mikrotik_group=plan.mikrotik_group,
        address_list=plan.address_list,
        session_timeout_s=plan.session_timeout_s,
        idle_timeout_s=plan.idle_timeout_s,
        interim_interval_s=plan.interim_interval_s,
        enforce_quota_on_nas=bool(plan.enforce_quota_on_nas),
        custom_attributes=tuple(plan.custom_attributes or []),
        default_concurrent_sessions=plan.default_concurrent_sessions or 1,
    )


def daily_policy_spec(policy: DailyPolicy) -> DailyPolicySpec:
    return DailyPolicySpec(
        counted=policy.counted or "rxtx",
        tiers=tuple(FupTier(**t) for t in (policy.tiers or [])),
        daily_quota_bytes=policy.daily_quota_bytes,
        on_exhaust=policy.on_exhaust or "throttle",
        exhausted_rx_bps=policy.exhausted_rx_bps,
        exhausted_tx_bps=policy.exhausted_tx_bps,
        free_windows=tuple(
            FreeWindow(
                start=parse_hhmm(w["start"]),
                end=parse_hhmm(w["end"]),
                days=frozenset(w.get("days") or WEEKDAY_NAMES),
            )
            for w in (policy.free_windows or [])
        ),
    )


def subscriber_spec(s: Subscriber) -> SubscriberSpec:
    return SubscriberSpec(
        enabled=s.enabled,
        mac_binding=s.mac_binding,
        static_ip=str(s.static_ip) if s.static_ip else None,
        rx_limit_override_bps=s.rx_limit_override_bps,
        tx_limit_override_bps=s.tx_limit_override_bps,
        custom_attributes=tuple(s.custom_attributes or []),
        exempt_time_of_day=bool(s.exempt_time_of_day),
        exempt_fup=bool(s.exempt_fup),
        concurrent_session_limit=s.concurrent_session_limit,
    )


async def load_usage(db: AsyncSession, subscriber_id, period_start: datetime | None, now: datetime) -> UsageSnapshot:
    today = policy_day(now)
    first_day = policy_day(period_start) if period_start else today
    rows = (
        await db.execute(
            select(UsageDaily).where(UsageDaily.subscriber_id == subscriber_id, UsageDaily.day >= first_day)
        )
    ).scalars().all()
    snap = {"today_counted_rx": 0, "today_counted_tx": 0, "period_counted_rx": 0, "period_counted_tx": 0}
    for r in rows:
        snap["period_counted_rx"] += r.counted_rx_bytes
        snap["period_counted_tx"] += r.counted_tx_bytes
        if r.day == today:
            snap["today_counted_rx"] = r.counted_rx_bytes
            snap["today_counted_tx"] = r.counted_tx_bytes
    return UsageSnapshot(**snap)


async def count_other_live_sessions(
    db: AsyncSession, subscriber_id, calling_station_id: str | None, now: datetime
) -> int:
    """Live = no Stop seen and an accounting packet within the stale window.
    A session from the same CPE MAC isn't counted: that's the router
    reconnecting after a lost Stop, not a second user."""
    q = select(func.count()).select_from(RadiusSession).where(
        RadiusSession.subscriber_id == subscriber_id,
        RadiusSession.stopped_at.is_(None),
        RadiusSession.updated_at > now - timedelta(seconds=settings.session_stale_after_s),
    )
    if calling_station_id:
        q = q.where(
            (RadiusSession.calling_station_id.is_(None))
            | (func.lower(RadiusSession.calling_station_id) != calling_station_id.lower())
        )
    return (await db.execute(q)).scalar_one()


async def build_inputs(
    db: AsyncSession,
    subscriber: Subscriber,
    now: datetime,
    calling_station_id: str | None = None,
    check_concurrency: bool = True,
) -> PolicyInputs | AuthorizeDecision:
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

    daily = None
    if plan.daily_policy_id is not None:
        dp = (await db.execute(select(DailyPolicy).where(DailyPolicy.id == plan.daily_policy_id))).scalar_one_or_none()
        daily = daily_policy_spec(dp) if dp else None

    windows: tuple = ()
    if plan.time_of_day_policy_id is not None:
        policy = (
            await db.execute(select(TimeOfDayPolicy).where(TimeOfDayPolicy.id == plan.time_of_day_policy_id))
        ).scalar_one_or_none()
        windows = tuple(parse_window(w) for w in (policy.windows if policy else []))

    usage = await load_usage(db, subscriber.id, subscription.period_started_at or subscription.started_at, now)

    other_live = (
        await count_other_live_sessions(db, subscriber.id, calling_station_id, now) if check_concurrency else None
    )

    return PolicyInputs(
        subscriber=subscriber_spec(subscriber),
        plan=plan_spec(plan),
        now=now,
        local_now=to_local(now),
        expiration=subscription.expiration,
        daily=daily,
        tod_windows=windows,
        quota_remaining={(row.kind, row.period): row.remaining for row in quota_rows},
        usage=usage,
        calling_station_id=calling_station_id,
        other_live_sessions=other_live,
        walled_garden_list=settings.walled_garden_address_list,
        walled_garden_rx_bps=settings.walled_garden_rx_bps,
        walled_garden_tx_bps=settings.walled_garden_tx_bps,
    )


async def decision_for_subscriber(
    db: AsyncSession,
    subscriber: Subscriber,
    now: datetime,
    calling_station_id: str | None = None,
    check_concurrency: bool = False,
) -> AuthorizeDecision:
    inputs = await build_inputs(db, subscriber, now, calling_station_id, check_concurrency)
    if isinstance(inputs, AuthorizeDecision):
        return inputs
    return decide(inputs)


async def compute_authorize_decision(
    db: AsyncSession, username: str, calling_station_id: str | None, now: datetime
) -> AuthorizeDecision:
    subscriber = (
        await db.execute(select(Subscriber).where(Subscriber.username == username))
    ).scalar_one_or_none()
    if subscriber is None:
        return AuthorizeDecision(accepted=False, reply_attributes={}, reason="no_such_subscriber")
    return await decision_for_subscriber(db, subscriber, now, calling_station_id, check_concurrency=True)
