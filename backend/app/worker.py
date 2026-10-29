"""Background worker: everything time-driven that RADIUS itself won't trigger.

One process (Redis lock enforces it), ticking every settings.worker_tick_s:

  stale sweep        sessions with no accounting past the stale window -> closed
  expiry sweep       live sessions whose subscription just expired -> reconcile
  scheduled changes  next_profile_change_at reached -> switch plan, reprovision
  daily reset        daily quota rows past resets_at -> refilled; at each new
                     policy day every live subscriber is reconciled (daily FUP
                     lifts at midnight)
  monthly reset      calendar-mode plans: monthly quota rows past resets_at
  window edges       any time-of-day policy whose active-window set changed ->
                     reconcile its live subscribers (the 17:00 peak CoA)
  reconcile queue    drain subscriber ids queued by accounting / admin edits
  auth log prune     once per policy day

Each step is a plain async function taking (db, redis, now) so tests drive
them directly with a frozen clock.

Run: python -m app.worker
"""

import asyncio
import logging
import os
import socket
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select

from app.actuation.coa import CoASender, PyradCoASender
from app.actuation.service import drain, enqueue, reconcile_subscriber
from app.config import settings
from app.db import SessionLocal
from app.models.audit import AuditLog
from app.models.plan import Plan, Subscription, TimeOfDayPolicy
from app.models.quota import QuotaState
from app.models.session import RadiusSession
from app.models.usage import AuthEvent
from app.policy.clock import next_daily_reset, next_month_start_utc, policy_day, to_local
from app.policy.quota_engine import QuotaPeriod
from app.policy.time_of_day import active_signature, parse_window
from app.redis_client import get_redis
from app.services.subscriptions import change_plan, plan_quota

logger = logging.getLogger("engine.worker")

LOCK_KEY = "worker:lock"
TOD_SIG_KEY = "worker:tod_signature"
DAILY_DAY_KEY = "worker:last_policy_day"


async def _live_subscriber_ids(db, now: datetime, plan_ids: set | None = None) -> set:
    q = (
        select(RadiusSession.subscriber_id)
        .where(
            RadiusSession.stopped_at.is_(None),
            RadiusSession.updated_at > now - timedelta(seconds=settings.session_stale_after_s),
        )
        .distinct()
    )
    if plan_ids is not None:
        if not plan_ids:
            return set()
        q = q.join(Subscription, Subscription.subscriber_id == RadiusSession.subscriber_id).where(
            Subscription.plan_id.in_(plan_ids)
        )
    return set((await db.execute(q)).scalars().all())


async def sweep_stale_sessions(db, redis, now: datetime) -> int:
    cutoff = now - timedelta(seconds=settings.session_stale_after_s)
    rows = (
        await db.execute(
            select(RadiusSession).where(RadiusSession.stopped_at.is_(None), RadiusSession.updated_at <= cutoff)
        )
    ).scalars().all()
    for row in rows:
        row.stopped_at = now
        row.terminate_cause = "Stale"
    return len(rows)


async def sweep_expired(db, redis, now: datetime) -> int:
    live = await _live_subscriber_ids(db, now)
    if not live:
        return 0
    expired = set((
        await db.execute(
            select(Subscription.subscriber_id).where(
                Subscription.subscriber_id.in_(live),
                Subscription.expiration.is_not(None),
                Subscription.expiration <= now,
            )
        )
    ).scalars().all())
    await enqueue(redis, *expired)
    return len(expired)


async def apply_scheduled_changes(db, redis, now: datetime) -> int:
    due = (
        await db.execute(
            select(Subscription).where(
                Subscription.next_profile_change_at.is_not(None),
                Subscription.next_profile_change_at <= now,
            )
        )
    ).scalars().all()
    for sub in due:
        new_plan = await db.get(Plan, sub.next_profile_change_plan_id) if sub.next_profile_change_plan_id else None
        old_plan_id = sub.plan_id
        if new_plan is None:
            sub.next_profile_change_at = None
            sub.next_profile_change_plan_id = None
            continue
        await change_plan(db, sub, new_plan, now, reset_quota=True)
        db.add(AuditLog(tenant_id=new_plan.tenant_id, actor="system:scheduler", action="subscriber.change_profile",
                        subject=f"subscriber:{sub.subscriber_id}", old_value={"plan_id": str(old_plan_id)},
                        new_value={"plan_id": str(new_plan.id), "scheduled": True}))
        await enqueue(redis, sub.subscriber_id)
    return len(due)


async def reset_quotas(db, redis, now: datetime) -> dict:
    """Refill quota rows whose resets_at has passed. Data-driven (not "did we
    run today?"), so a worker that was down at midnight catches up on start.
    Monthly rows on renewal-mode plans are left alone — renewal resets them."""
    rows = (
        await db.execute(
            select(QuotaState, Plan)
            .join(Subscription, Subscription.subscriber_id == QuotaState.subscriber_id)
            .join(Plan, Plan.id == Subscription.plan_id)
            .where(QuotaState.resets_at <= now)
        )
    ).all()
    daily = monthly = 0
    touched = set()
    for quota, plan in rows:
        allowance = plan_quota(plan, quota.kind, quota.period)
        if quota.period == QuotaPeriod.DAILY:
            if allowance is not None:
                quota.remaining = allowance
            quota.resets_at = next_daily_reset(now)
            daily += 1
        else:
            if plan.monthly_reset != "calendar":
                continue
            if allowance is not None:
                quota.remaining = allowance
            quota.resets_at = next_month_start_utc(now)
            monthly += 1
            sub = await db.get(Subscription, quota.subscriber_id)
            if sub is not None:
                sub.period_started_at = now
        quota.updated_at = now
        touched.add(quota.subscriber_id)
    await enqueue(redis, *touched)

    # New policy day: daily FUP and daily caps lift for everyone online.
    today = policy_day(now).isoformat()
    if await redis.get(DAILY_DAY_KEY) != today:
        await redis.set(DAILY_DAY_KEY, today)
        await enqueue(redis, *(await _live_subscriber_ids(db, now)))
    return {"daily": daily, "monthly": monthly}


async def detect_window_edges(db, redis, now: datetime) -> int:
    local = to_local(now)
    policies = (await db.execute(select(TimeOfDayPolicy))).scalars().all()
    changed_policy_ids = []
    for policy in policies:
        sig = active_signature([parse_window(w) for w in (policy.windows or [])], local)
        prev = await redis.hget(TOD_SIG_KEY, str(policy.id))
        if prev != sig:
            await redis.hset(TOD_SIG_KEY, str(policy.id), sig)
            if prev is not None:  # first sighting just records state
                changed_policy_ids.append(policy.id)
    if not changed_policy_ids:
        return 0
    plan_ids = set((
        await db.execute(select(Plan.id).where(Plan.time_of_day_policy_id.in_(changed_policy_ids)))
    ).scalars().all())
    subs = await _live_subscriber_ids(db, now, plan_ids)
    await enqueue(redis, *subs)
    return len(subs)


async def prune_auth_log(db, redis, now: datetime) -> int:
    key = "worker:last_prune_day"
    today = policy_day(now).isoformat()
    if await redis.get(key) == today:
        return 0
    await redis.set(key, today)
    result = await db.execute(
        delete(AuthEvent).where(AuthEvent.ts < now - timedelta(days=settings.auth_log_retention_days))
    )
    return result.rowcount or 0


async def drain_reconcile_queue(session_factory, redis, sender: CoASender, now: datetime, limit: int = 200) -> dict:
    ids = await drain(redis, limit)
    summary = {"subscribers": len(ids), "coa": 0, "disconnect": 0, "noop": 0, "skipped": 0}
    for sid in ids:
        async with session_factory() as db:
            try:
                results = await reconcile_subscriber(db, redis, sender, sid, now)
                await db.commit()
            except Exception:
                logger.exception("reconcile failed for %s", sid)
                await db.rollback()
                continue
        for r in results:
            summary[r.action] = summary.get(r.action, 0) + 1
    return summary


async def run_tick(session_factory, redis, sender: CoASender, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    out: dict = {}
    async with session_factory() as db:
        out["stale_closed"] = await sweep_stale_sessions(db, redis, now)
        out["scheduled_changes"] = await apply_scheduled_changes(db, redis, now)
        out["quota_resets"] = await reset_quotas(db, redis, now)
        out["expired_queued"] = await sweep_expired(db, redis, now)
        out["window_edge_queued"] = await detect_window_edges(db, redis, now)
        out["auth_log_pruned"] = await prune_auth_log(db, redis, now)
        await db.commit()
    out["reconcile"] = await drain_reconcile_queue(session_factory, redis, sender, now)
    return out


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    redis = get_redis()
    sender = PyradCoASender(timeout_s=settings.coa_timeout_s, retries=settings.coa_retries)
    me = f"{socket.gethostname()}:{os.getpid()}"
    lock_ttl = max(60, settings.worker_tick_s * 4)
    logger.info("worker starting (dry_run=%s, tick=%ss)", settings.actuation_dry_run, settings.worker_tick_s)
    while True:
        got = await redis.set(LOCK_KEY, me, nx=True, ex=lock_ttl) or await redis.get(LOCK_KEY) == me
        if got:
            await redis.expire(LOCK_KEY, lock_ttl)
            try:
                summary = await run_tick(SessionLocal, redis, sender)
                if any(v for k, v in summary.items() if k != "reconcile") or summary["reconcile"]["subscribers"]:
                    logger.info("tick %s", summary)
            except Exception:
                logger.exception("tick failed")
        await asyncio.sleep(settings.worker_tick_s)


if __name__ == "__main__":
    asyncio.run(main())
