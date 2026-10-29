"""Profiles, daily policies and time-of-day policies."""

import re
import uuid
from datetime import timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select

from app.actuation.service import enqueue
from app.api.admin.deps import AdminCtx, admin_ctx
from app.api.admin.util import changes, jsonable, live_subscribers_on_plans
from app.models.plan import DailyPolicy, Plan, Subscription, TimeOfDayPolicy
from app.policy.authorize_service import daily_policy_spec, plan_spec
from app.policy.clock import to_local
from app.policy.decision import PolicyInputs, SubscriberSpec, UsageSnapshot, decide
from app.policy.rate_limit import format_rate_limit
from app.policy.time_of_day import parse_window, window_active
from app.schemas.admin import (
    DailyPolicyIn,
    DailyPolicyOut,
    DailyPolicyPatch,
    ProfileIn,
    ProfileOut,
    ProfilePatch,
    SimulateIn,
    TimePolicyIn,
    TimePolicyOut,
    TimePolicyPatch,
)
from app.config import settings

router = APIRouter()


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:80] or "profile"


# --- profiles ----------------------------------------------------------------

async def _profile_out(ctx: AdminCtx, plan: Plan) -> ProfileOut:
    count = (await ctx.db.execute(
        select(func.count()).select_from(Subscription).where(Subscription.plan_id == plan.id)
    )).scalar_one()
    out = ProfileOut.model_validate(plan)
    out.subscriber_count = count
    out.rate_limit_preview = format_rate_limit(plan_spec(plan).rate)
    return out


async def _get_plan(ctx: AdminCtx, profile_id: uuid.UUID) -> Plan:
    plan = await ctx.db.get(Plan, profile_id)
    if plan is None or plan.tenant_id != ctx.tenant_id:
        raise HTTPException(404, "profile not found")
    return plan


async def _check_refs(ctx: AdminCtx, data: dict) -> None:
    if data.get("daily_policy_id"):
        dp = await ctx.db.get(DailyPolicy, data["daily_policy_id"])
        if dp is None or dp.tenant_id != ctx.tenant_id:
            raise HTTPException(422, "daily_policy_id does not exist")
    if data.get("time_of_day_policy_id"):
        tp = await ctx.db.get(TimeOfDayPolicy, data["time_of_day_policy_id"])
        if tp is None or tp.tenant_id != ctx.tenant_id:
            raise HTTPException(422, "time_of_day_policy_id does not exist")


def _plan_values(data: dict) -> dict:
    data = dict(data)
    if "custom_attributes" in data and data["custom_attributes"] is not None:
        data["custom_attributes"] = [dict(a) for a in data["custom_attributes"]]
    if "fup_tiers" in data and data["fup_tiers"] is not None:
        data["fup_tiers"] = [dict(t) for t in data["fup_tiers"]]
    return data


@router.get("/profiles", response_model=list[ProfileOut])
async def list_profiles(ctx: AdminCtx = Depends(admin_ctx)):
    plans = (await ctx.db.execute(select(Plan).where(Plan.tenant_id == ctx.tenant_id).order_by(Plan.name))).scalars().all()
    return [await _profile_out(ctx, p) for p in plans]


@router.post("/profiles", response_model=ProfileOut, status_code=201)
async def create_profile(body: ProfileIn, ctx: AdminCtx = Depends(admin_ctx)):
    data = _plan_values(body.model_dump())
    await _check_refs(ctx, data)
    data["profile_name_normalized"] = data.get("profile_name_normalized") or _slug(body.name)
    plan = Plan(tenant_id=ctx.tenant_id, **data)
    ctx.db.add(plan)
    await ctx.db.flush()
    ctx.audit("profile.create", f"profile:{plan.id}", None, jsonable(data))
    await ctx.db.commit()
    return await _profile_out(ctx, plan)


@router.get("/profiles/{profile_id}", response_model=ProfileOut)
async def get_profile(profile_id: uuid.UUID, ctx: AdminCtx = Depends(admin_ctx)):
    return await _profile_out(ctx, await _get_plan(ctx, profile_id))


@router.patch("/profiles/{profile_id}", response_model=ProfileOut)
async def update_profile(profile_id: uuid.UUID, body: ProfilePatch, ctx: AdminCtx = Depends(admin_ctx)):
    plan = await _get_plan(ctx, profile_id)
    data = _plan_values(body.model_dump(exclude_unset=True))
    for required in ("name", "rx_limit_bps", "tx_limit_bps"):
        if required in data and data[required] is None:
            raise HTTPException(422, f"{required} cannot be null")
    await _check_refs(ctx, data)
    old, new = changes(plan, data)
    for k, v in data.items():
        setattr(plan, k, v)
    ctx.audit("profile.update", f"profile:{plan.id}", old, new)
    await ctx.db.commit()
    # Everyone online on this profile may now be owed different attributes.
    await enqueue(ctx.redis, *(await live_subscribers_on_plans(ctx.db, {plan.id}, ctx.now)))
    return await _profile_out(ctx, plan)


@router.delete("/profiles/{profile_id}", status_code=204)
async def delete_profile(profile_id: uuid.UUID, ctx: AdminCtx = Depends(admin_ctx)):
    plan = await _get_plan(ctx, profile_id)
    in_use = (await ctx.db.execute(
        select(func.count()).select_from(Subscription).where(
            (Subscription.plan_id == plan.id) | (Subscription.next_profile_change_plan_id == plan.id)
        )
    )).scalar_one()
    if in_use:
        raise HTTPException(409, f"{in_use} subscriber(s) use or are scheduled onto this profile")
    ctx.audit("profile.delete", f"profile:{plan.id}", {"name": plan.name}, None)
    await ctx.db.delete(plan)
    await ctx.db.commit()


@router.post("/profiles/{profile_id}/simulate")
async def simulate_profile(profile_id: uuid.UUID, body: SimulateIn, ctx: AdminCtx = Depends(admin_ctx)):
    """What a healthy subscriber on this profile would get at `at` with the
    given usage — for checking peak windows and FUP tiers before rollout."""
    plan = await _get_plan(ctx, profile_id)
    at = body.at or ctx.now
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    daily = None
    if plan.daily_policy_id:
        dp = await ctx.db.get(DailyPolicy, plan.daily_policy_id)
        daily = daily_policy_spec(dp) if dp else None
    windows: tuple = ()
    if plan.time_of_day_policy_id:
        tp = await ctx.db.get(TimeOfDayPolicy, plan.time_of_day_policy_id)
        windows = tuple(parse_window(w) for w in (tp.windows if tp else []))
    decision = decide(PolicyInputs(
        subscriber=SubscriberSpec(),
        plan=plan_spec(plan),
        now=at,
        local_now=to_local(at),
        expiration=None,
        daily=daily,
        tod_windows=windows,
        usage=UsageSnapshot(today_counted_tx=body.today_bytes, period_counted_tx=body.period_bytes),
        walled_garden_list=settings.walled_garden_address_list,
        walled_garden_rx_bps=settings.walled_garden_rx_bps,
        walled_garden_tx_bps=settings.walled_garden_tx_bps,
    ))
    return {"at": at, "local_time": to_local(at).isoformat(), "accepted": decision.accepted,
            "reason": decision.reason, "reply_attributes": decision.reply_attributes, "explain": decision.explain}


# --- daily policies ----------------------------------------------------------

async def _get_daily(ctx: AdminCtx, policy_id: uuid.UUID) -> DailyPolicy:
    dp = await ctx.db.get(DailyPolicy, policy_id)
    if dp is None or dp.tenant_id != ctx.tenant_id:
        raise HTTPException(404, "daily policy not found")
    return dp


async def _daily_out(ctx: AdminCtx, dp: DailyPolicy) -> DailyPolicyOut:
    out = DailyPolicyOut.model_validate(dp)
    out.profile_count = (await ctx.db.execute(
        select(func.count()).select_from(Plan).where(Plan.daily_policy_id == dp.id)
    )).scalar_one()
    return out


def _daily_values(data: dict) -> dict:
    data = dict(data)
    for key in ("tiers", "free_windows"):
        if data.get(key) is not None:
            data[key] = [dict(x) for x in data[key]]
    return data


@router.get("/daily-policies", response_model=list[DailyPolicyOut])
async def list_daily(ctx: AdminCtx = Depends(admin_ctx)):
    rows = (await ctx.db.execute(
        select(DailyPolicy).where(DailyPolicy.tenant_id == ctx.tenant_id).order_by(DailyPolicy.name)
    )).scalars().all()
    return [await _daily_out(ctx, r) for r in rows]


@router.post("/daily-policies", response_model=DailyPolicyOut, status_code=201)
async def create_daily(body: DailyPolicyIn, ctx: AdminCtx = Depends(admin_ctx)):
    data = _daily_values(body.model_dump())
    dp = DailyPolicy(tenant_id=ctx.tenant_id, **data)
    ctx.db.add(dp)
    await ctx.db.flush()
    ctx.audit("daily_policy.create", f"daily_policy:{dp.id}", None, jsonable(data))
    await ctx.db.commit()
    return await _daily_out(ctx, dp)


@router.get("/daily-policies/{policy_id}", response_model=DailyPolicyOut)
async def get_daily(policy_id: uuid.UUID, ctx: AdminCtx = Depends(admin_ctx)):
    return await _daily_out(ctx, await _get_daily(ctx, policy_id))


@router.patch("/daily-policies/{policy_id}", response_model=DailyPolicyOut)
async def update_daily(policy_id: uuid.UUID, body: DailyPolicyPatch, ctx: AdminCtx = Depends(admin_ctx)):
    dp = await _get_daily(ctx, policy_id)
    data = _daily_values(body.model_dump(exclude_unset=True))
    if data.get("name", "x") is None:
        raise HTTPException(422, "name cannot be null")
    for key in ("tiers", "free_windows"):
        if key in data and data[key] is None:
            data[key] = []
    old, new = changes(dp, data)
    for k, v in data.items():
        setattr(dp, k, v)
    ctx.audit("daily_policy.update", f"daily_policy:{dp.id}", old, new)
    await ctx.db.commit()
    plan_ids = set((await ctx.db.execute(select(Plan.id).where(Plan.daily_policy_id == dp.id))).scalars().all())
    await enqueue(ctx.redis, *(await live_subscribers_on_plans(ctx.db, plan_ids, ctx.now)))
    return await _daily_out(ctx, dp)


@router.delete("/daily-policies/{policy_id}", status_code=204)
async def delete_daily(policy_id: uuid.UUID, ctx: AdminCtx = Depends(admin_ctx)):
    dp = await _get_daily(ctx, policy_id)
    used = (await ctx.db.execute(select(func.count()).select_from(Plan).where(Plan.daily_policy_id == dp.id))).scalar_one()
    if used:
        raise HTTPException(409, f"{used} profile(s) use this daily policy")
    ctx.audit("daily_policy.delete", f"daily_policy:{dp.id}", {"name": dp.name}, None)
    await ctx.db.delete(dp)
    await ctx.db.commit()


# --- time-of-day policies ----------------------------------------------------

async def _get_tod(ctx: AdminCtx, policy_id: uuid.UUID) -> TimeOfDayPolicy:
    tp = await ctx.db.get(TimeOfDayPolicy, policy_id)
    if tp is None or tp.tenant_id != ctx.tenant_id:
        raise HTTPException(404, "time policy not found")
    return tp


async def _tod_out(ctx: AdminCtx, tp: TimeOfDayPolicy) -> TimePolicyOut:
    out = TimePolicyOut.model_validate(tp)
    local = to_local(ctx.now)
    out.active_now = [
        i for i, w in enumerate(parse_window(x) for x in (tp.windows or []))
        if window_active(w.start, w.end, w.days, local)
    ]
    out.profile_count = (await ctx.db.execute(
        select(func.count()).select_from(Plan).where(Plan.time_of_day_policy_id == tp.id)
    )).scalar_one()
    return out


@router.get("/time-policies", response_model=list[TimePolicyOut])
async def list_tod(ctx: AdminCtx = Depends(admin_ctx)):
    rows = (await ctx.db.execute(
        select(TimeOfDayPolicy).where(TimeOfDayPolicy.tenant_id == ctx.tenant_id).order_by(TimeOfDayPolicy.name)
    )).scalars().all()
    return [await _tod_out(ctx, r) for r in rows]


@router.post("/time-policies", response_model=TimePolicyOut, status_code=201)
async def create_tod(body: TimePolicyIn, ctx: AdminCtx = Depends(admin_ctx)):
    data = {"name": body.name, "windows": [w.model_dump() for w in body.windows]}
    tp = TimeOfDayPolicy(tenant_id=ctx.tenant_id, **data)
    ctx.db.add(tp)
    await ctx.db.flush()
    ctx.audit("time_policy.create", f"time_policy:{tp.id}", None, data)
    await ctx.db.commit()
    return await _tod_out(ctx, tp)


@router.get("/time-policies/{policy_id}", response_model=TimePolicyOut)
async def get_tod(policy_id: uuid.UUID, ctx: AdminCtx = Depends(admin_ctx)):
    return await _tod_out(ctx, await _get_tod(ctx, policy_id))


@router.patch("/time-policies/{policy_id}", response_model=TimePolicyOut)
async def update_tod(policy_id: uuid.UUID, body: TimePolicyPatch, ctx: AdminCtx = Depends(admin_ctx)):
    tp = await _get_tod(ctx, policy_id)
    data = body.model_dump(exclude_unset=True)
    if data.get("name", "x") is None:
        raise HTTPException(422, "name cannot be null")
    if "windows" in data:
        data["windows"] = [dict(w) for w in (data["windows"] or [])]
    old, new = changes(tp, data)
    for k, v in data.items():
        setattr(tp, k, v)
    ctx.audit("time_policy.update", f"time_policy:{tp.id}", old, new)
    await ctx.db.commit()
    plan_ids = set((await ctx.db.execute(select(Plan.id).where(Plan.time_of_day_policy_id == tp.id))).scalars().all())
    await enqueue(ctx.redis, *(await live_subscribers_on_plans(ctx.db, plan_ids, ctx.now)))
    return await _tod_out(ctx, tp)


@router.delete("/time-policies/{policy_id}", status_code=204)
async def delete_tod(policy_id: uuid.UUID, ctx: AdminCtx = Depends(admin_ctx)):
    tp = await _get_tod(ctx, policy_id)
    used = (await ctx.db.execute(
        select(func.count()).select_from(Plan).where(Plan.time_of_day_policy_id == tp.id)
    )).scalar_one()
    if used:
        raise HTTPException(409, f"{used} profile(s) use this time policy")
    ctx.audit("time_policy.delete", f"time_policy:{tp.id}", {"name": tp.name}, None)
    await ctx.db.delete(tp)
    await ctx.db.commit()
