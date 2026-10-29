"""Subscription lifecycle: quota provisioning, renewal, profile change. Shared
by the admin API and the worker so "renew" means the same thing whether an
operator clicked it in Atlas or a scheduled change fired at 03:00.
"""

from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.plan import Plan, Subscription
from app.models.quota import QuotaState
from app.policy.clock import next_daily_reset, next_month_start_utc
from app.policy.quota_engine import QuotaKind, QuotaPeriod

_PLAN_QUOTA_FIELD = {
    (QuotaKind.RX, QuotaPeriod.MONTHLY): "quota_rx_monthly",
    (QuotaKind.TX, QuotaPeriod.MONTHLY): "quota_tx_monthly",
    (QuotaKind.RXTX, QuotaPeriod.MONTHLY): "quota_rxtx_monthly",
    (QuotaKind.UPTIME, QuotaPeriod.MONTHLY): "quota_uptime_monthly_s",
    (QuotaKind.RX, QuotaPeriod.DAILY): "quota_rx_daily",
    (QuotaKind.TX, QuotaPeriod.DAILY): "quota_tx_daily",
    (QuotaKind.RXTX, QuotaPeriod.DAILY): "quota_rxtx_daily",
    (QuotaKind.UPTIME, QuotaPeriod.DAILY): "quota_uptime_daily_s",
}


def plan_quota(plan: Plan, kind: QuotaKind, period: QuotaPeriod) -> int | None:
    return getattr(plan, _PLAN_QUOTA_FIELD[(kind, period)])


def monthly_resets_at(plan: Plan, subscription: Subscription, now: datetime) -> datetime:
    if plan.monthly_reset == "calendar":
        return next_month_start_utc(now)
    return subscription.expiration or (now + timedelta(days=plan.validity_days or 30))


async def provision_quotas(
    db: AsyncSession, subscription: Subscription, plan: Plan, now: datetime,
    periods: tuple[QuotaPeriod, ...] = (QuotaPeriod.MONTHLY, QuotaPeriod.DAILY),
) -> None:
    """Make quota_state match the plan for the given periods: full allowance,
    fresh reset marker. Kinds the plan leaves unlimited get no row (no row =
    unlimited, which is what gates/quota_engine assume)."""
    existing = {
        (r.kind, r.period): r
        for r in (
            await db.execute(select(QuotaState).where(QuotaState.subscriber_id == subscription.subscriber_id))
        ).scalars().all()
    }
    for (kind, period), field in _PLAN_QUOTA_FIELD.items():
        if period not in periods:
            continue
        allowance = getattr(plan, field)
        row = existing.get((kind, period))
        if allowance is None:
            if row is not None:
                await db.delete(row)
            continue
        resets_at = next_daily_reset(now) if period == QuotaPeriod.DAILY else monthly_resets_at(plan, subscription, now)
        if row is None:
            db.add(QuotaState(subscriber_id=subscription.subscriber_id, kind=kind, period=period,
                              remaining=allowance, resets_at=resets_at, updated_at=now))
        else:
            row.remaining = allowance
            row.resets_at = resets_at
            row.updated_at = now
    if QuotaPeriod.MONTHLY in periods:
        subscription.period_started_at = now


async def renew(
    db: AsyncSession, subscription: Subscription, plan: Plan, now: datetime,
    days: int | None = None, reset_quota: bool | None = None,
) -> datetime:
    """Extend from whichever is later — now or the current expiry — so renewing
    early never loses paid days, and renewing late doesn't back-date."""
    base = max(now, subscription.expiration) if subscription.expiration else now
    subscription.expiration = base + timedelta(days=days or plan.validity_days or 30)
    if reset_quota is None:
        reset_quota = plan.monthly_reset == "renewal"
    if reset_quota:
        await provision_quotas(db, subscription, plan, now)
    return subscription.expiration


async def change_plan(
    db: AsyncSession, subscription: Subscription, new_plan: Plan, now: datetime, reset_quota: bool = True,
) -> None:
    subscription.plan_id = new_plan.id
    subscription.next_profile_change_plan_id = None
    subscription.next_profile_change_at = None
    if reset_quota:
        await provision_quotas(db, subscription, new_plan, now)
    else:
        # Keep consumption, but drop rows for kinds the new plan doesn't meter
        # and add rows for kinds it newly meters.
        await provision_quotas_missing_only(db, subscription, new_plan, now)


async def provision_quotas_missing_only(db: AsyncSession, subscription: Subscription, plan: Plan, now: datetime) -> None:
    existing = {
        (r.kind, r.period): r
        for r in (
            await db.execute(select(QuotaState).where(QuotaState.subscriber_id == subscription.subscriber_id))
        ).scalars().all()
    }
    for (kind, period), field in _PLAN_QUOTA_FIELD.items():
        allowance = getattr(plan, field)
        row = existing.get((kind, period))
        if allowance is None and row is not None:
            await db.delete(row)
        elif allowance is not None and row is None:
            resets_at = next_daily_reset(now) if period == QuotaPeriod.DAILY else monthly_resets_at(plan, subscription, now)
            db.add(QuotaState(subscriber_id=subscription.subscriber_id, kind=kind, period=period,
                              remaining=allowance, resets_at=resets_at, updated_at=now))


async def clear_quotas(db: AsyncSession, subscriber_id) -> None:
    await db.execute(delete(QuotaState).where(QuotaState.subscriber_id == subscriber_id))
