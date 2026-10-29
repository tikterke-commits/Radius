"""Subscribers: CRUD, lifecycle actions, and the detail view Atlas renders."""

import secrets
import string
import uuid
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import delete, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.actuation.coa import CoASender
from app.actuation.service import disconnect_subscriber, enqueue, live_sessions, reconcile_subscriber
from app.api.admin.deps import AdminCtx, admin_ctx, get_sender
from app.api.admin.util import changes, jsonable
from app.config import settings
from app.models.billing import AddonSubscription, Balance, Invoice
from app.models.credential import Credential
from app.models.plan import Plan, Subscription, SubscriberPolicyException
from app.models.quota import GateStatus, QuotaState
from app.models.session import RadiusSession
from app.models.subscriber import Subscriber
from app.models.usage import AuthEvent, UsageDaily
from app.policy.authorize_service import build_inputs
from app.policy.clock import policy_day
from app.policy.decision import PolicyInputs, decide
from app.policy.gates import SubscriberSnapshot, evaluate_gates
from app.policy.quota_engine import QuotaKind, QuotaPeriod
from app.schemas.admin import (
    BulkIn,
    ChangeProfileIn,
    MacIn,
    Page,
    PasswordIn,
    QuotaResetIn,
    RenewIn,
    SubscriberCreate,
    SubscriberPatch,
    SubscriberRow,
    TopUpIn,
    UsageDay,
    normalize_mac,
)
from app.security.credentials import hash_password
from app.services.subscriptions import change_plan, plan_quota, provision_quotas, renew

router = APIRouter()

_ALPHABET = string.ascii_letters + string.digits


def _generate_password(length: int = 10) -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(length))


def _live_clause(now):
    return exists().where(
        RadiusSession.subscriber_id == Subscriber.id,
        RadiusSession.stopped_at.is_(None),
        RadiusSession.updated_at > now - timedelta(seconds=settings.session_stale_after_s),
    )


def _status(sub: Subscriber, expiration, now) -> str:
    if not sub.enabled:
        return "disabled"
    if expiration is not None and expiration <= now:
        return "expired"
    return "active"


def _row(sub: Subscriber, subscription: Subscription | None, plan: Plan | None, online: bool, now) -> SubscriberRow:
    exp = subscription.expiration if subscription else None
    return SubscriberRow(
        id=sub.id, username=sub.username, firstname=sub.firstname, lastname=sub.lastname, phone=sub.phone,
        city=sub.city, profile_id=plan.id if plan else None, profile_name=plan.name if plan else None,
        expiration=exp, status=_status(sub, exp, now), online=online,
        static_ip=str(sub.static_ip) if sub.static_ip else None, mac_binding=sub.mac_binding,
        last_online=sub.last_online, created_at=sub.created_at,
    )


async def _get_sub(ctx: AdminCtx, subscriber_id: uuid.UUID) -> Subscriber:
    sub = await ctx.db.get(Subscriber, subscriber_id)
    if sub is None or sub.tenant_id != ctx.tenant_id:
        raise HTTPException(404, "subscriber not found")
    return sub


async def _get_plan(ctx: AdminCtx, profile_id: uuid.UUID) -> Plan:
    plan = await ctx.db.get(Plan, profile_id)
    if plan is None or plan.tenant_id != ctx.tenant_id:
        raise HTTPException(422, "profile not found")
    return plan


async def _subscription(db: AsyncSession, subscriber_id) -> Subscription:
    s = await db.get(Subscription, subscriber_id)
    if s is None:
        raise HTTPException(409, "subscriber has no subscription")
    return s


def _sub_values(data: dict) -> dict:
    data = dict(data)
    if data.get("static_ip") is not None:
        data["static_ip"] = str(data["static_ip"])
    if data.get("custom_attributes") is not None:
        data["custom_attributes"] = [dict(a) for a in data["custom_attributes"]]
    return data


# --- list / create -----------------------------------------------------------

@router.get("/subscribers", response_model=Page)
async def list_subscribers(
    q: str | None = None,
    status: str | None = Query(default=None, pattern="^(active|expired|disabled)$"),
    online: bool | None = None,
    profile_id: uuid.UUID | None = None,
    expiring_within_days: int | None = Query(default=None, ge=0, le=365),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500),
    sort: str = Query(default="username", pattern="^-?(username|expiration|created_at|last_online)$"),
    ctx: AdminCtx = Depends(admin_ctx),
):
    now = ctx.now
    live = _live_clause(now)
    stmt = (
        select(Subscriber, Subscription, Plan, live.label("online"))
        .outerjoin(Subscription, Subscription.subscriber_id == Subscriber.id)
        .outerjoin(Plan, Plan.id == Subscription.plan_id)
        .where(Subscriber.tenant_id == ctx.tenant_id)
    )
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(
            Subscriber.username.ilike(like), Subscriber.firstname.ilike(like), Subscriber.lastname.ilike(like),
            Subscriber.phone.ilike(like), Subscriber.mac_binding.ilike(like), Subscriber.notes.ilike(like),
        ))
    if status == "disabled":
        stmt = stmt.where(Subscriber.enabled.is_(False))
    elif status == "expired":
        stmt = stmt.where(Subscriber.enabled.is_(True), Subscription.expiration <= now)
    elif status == "active":
        stmt = stmt.where(Subscriber.enabled.is_(True),
                          or_(Subscription.expiration.is_(None), Subscription.expiration > now))
    if online is not None:
        stmt = stmt.where(live if online else ~live)
    if profile_id:
        stmt = stmt.where(Subscription.plan_id == profile_id)
    if expiring_within_days is not None:
        stmt = stmt.where(Subscription.expiration > now,
                          Subscription.expiration <= now + timedelta(days=expiring_within_days))

    total = (await ctx.db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    desc = sort.startswith("-")
    col = {"username": Subscriber.username, "expiration": Subscription.expiration,
           "created_at": Subscriber.created_at, "last_online": Subscriber.last_online}[sort.lstrip("-")]
    stmt = stmt.order_by(col.desc().nullslast() if desc else col.asc().nullslast(), Subscriber.username)
    rows = (await ctx.db.execute(stmt.offset((page - 1) * page_size).limit(page_size))).all()
    return Page(items=[_row(s, sn, p, bool(o), now) for s, sn, p, o in rows], total=total, page=page,
                page_size=page_size)


@router.post("/subscribers", status_code=201)
async def create_subscriber(body: SubscriberCreate, ctx: AdminCtx = Depends(admin_ctx)):
    exists_already = (await ctx.db.execute(select(Subscriber.id).where(Subscriber.username == body.username))).first()
    if exists_already:
        raise HTTPException(409, "username already exists")
    if not body.password and not body.generate_password:
        raise HTTPException(422, "password or generate_password is required")
    plan = await _get_plan(ctx, body.profile_id)
    password = body.password or _generate_password()

    data = _sub_values(body.model_dump(exclude={"password", "generate_password", "profile_id", "expiration",
                                                "validity_days", "username"}))
    sub = Subscriber(tenant_id=ctx.tenant_id, username=body.username, created_by=None, **data)
    ctx.db.add(sub)
    await ctx.db.flush()
    ctx.db.add(Credential(subscriber_id=sub.id, password_hash=hash_password(password)))
    expiration = body.expiration or (ctx.now + timedelta(days=body.validity_days or plan.validity_days or 30))
    subscription = Subscription(subscriber_id=sub.id, plan_id=plan.id, started_at=ctx.now, expiration=expiration,
                                period_started_at=ctx.now)
    ctx.db.add(subscription)
    await ctx.db.flush()
    await provision_quotas(ctx.db, subscription, plan, ctx.now)
    ctx.audit("subscriber.create", f"subscriber:{sub.id}", None,
              jsonable({"username": sub.username, "profile_id": plan.id, "expiration": expiration, **data}))
    await ctx.db.commit()
    out = await _detail(ctx, sub)
    if body.generate_password:
        out["generated_password"] = password  # shown once; only the hash is stored
    return out


# --- detail ------------------------------------------------------------------

async def _detail(ctx: AdminCtx, sub: Subscriber) -> dict:
    db, now = ctx.db, ctx.now
    subscription = await db.get(Subscription, sub.id)
    plan = await db.get(Plan, subscription.plan_id) if subscription else None
    next_plan = (await db.get(Plan, subscription.next_profile_change_plan_id)
                 if subscription and subscription.next_profile_change_plan_id else None)
    quotas = (await db.execute(select(QuotaState).where(QuotaState.subscriber_id == sub.id))).scalars().all()
    sessions = await live_sessions(db, sub.id, now)
    balance = await db.get(Balance, sub.id)
    credential = await db.get(Credential, sub.id)

    inputs = await build_inputs(db, sub, now, None, check_concurrency=False)
    if isinstance(inputs, PolicyInputs):
        decision = decide(inputs)
        usage = inputs.usage
        gates = evaluate_gates(SubscriberSnapshot(
            enabled=sub.enabled, expiration=inputs.expiration,
            quota_rx_remaining=_tight(inputs.quota_remaining, QuotaKind.RX),
            quota_tx_remaining=_tight(inputs.quota_remaining, QuotaKind.TX),
            quota_rxtx_remaining=_tight(inputs.quota_remaining, QuotaKind.RXTX),
            quota_uptime_remaining_s=_tight(inputs.quota_remaining, QuotaKind.UPTIME),
            now=now,
        ))
        gate_view = {"status": gates.status_gate, "traffic": gates.traffic_gate,
                     "expiration": gates.expiration_gate, "uptime": gates.uptime_gate}
    else:
        decision, usage, gate_view = inputs, None, None

    exp = subscription.expiration if subscription else None
    return {
        "subscriber": {
            **_row(sub, subscription, plan, bool(sessions), now).model_dump(),
            "company": sub.company, "email": sub.email, "address": sub.address, "notes": sub.notes,
            "national_id": sub.national_id, "contract_id": sub.contract_id,
            "concurrent_session_limit": sub.concurrent_session_limit,
            "rx_limit_override_bps": sub.rx_limit_override_bps, "tx_limit_override_bps": sub.tx_limit_override_bps,
            "custom_attributes": sub.custom_attributes or [], "exempt_time_of_day": sub.exempt_time_of_day,
            "exempt_fup": sub.exempt_fup, "enabled": sub.enabled,
            "password_rotation_required": bool(credential and credential.forced_rotation),
        },
        "subscription": None if subscription is None else {
            "profile_id": subscription.plan_id, "profile_name": plan.name if plan else None,
            "started_at": subscription.started_at, "expiration": exp,
            "days_left": None if exp is None else max(0, (exp - now).days),
            "period_started_at": subscription.period_started_at,
            "next_profile_change": None if not subscription.next_profile_change_at else {
                "profile_id": subscription.next_profile_change_plan_id,
                "profile_name": next_plan.name if next_plan else None,
                "at": subscription.next_profile_change_at,
            },
        },
        "gates": gate_view,
        "decision": {"accepted": decision.accepted, "reason": decision.reason,
                     "reply_attributes": decision.reply_attributes, "explain": decision.explain},
        "quotas": [{"kind": q.kind.value, "period": q.period.value, "remaining": q.remaining,
                    "allowance": plan_quota(plan, q.kind, q.period) if plan else None,
                    "resets_at": q.resets_at} for q in quotas],
        "usage": None if usage is None else {
            "policy_day": policy_day(now), "today_counted_rx": usage.today_counted_rx,
            "today_counted_tx": usage.today_counted_tx, "period_counted_rx": usage.period_counted_rx,
            "period_counted_tx": usage.period_counted_tx,
        },
        "balance": None if balance is None else {"balance": str(balance.balance), "loan_balance": str(balance.loan_balance)},
        "sessions": [_session_view(s) for s in sessions],
    }


def _tight(quota: dict, kind: QuotaKind):
    vals = [quota.get((kind, p)) for p in QuotaPeriod]
    vals = [v for v in vals if v is not None]
    return min(vals) if vals else None


def _session_view(s: RadiusSession) -> dict:
    return {
        "acct_session_id": s.acct_session_id, "username": s.username, "nas_ip": s.nas_ip,
        "framed_ip": s.framed_ip, "calling_station_id": s.calling_station_id, "nas_port_id": s.nas_port_id,
        "started_at": s.started_at, "updated_at": s.updated_at, "stopped_at": s.stopped_at,
        "session_time_s": s.last_session_time_s, "input_bytes": s.last_input_octets,
        "output_bytes": s.last_output_octets, "terminate_cause": s.terminate_cause,
        "rate_limit": (s.last_reply or {}).get("Mikrotik-Rate-Limit"),
        "address_list": (s.last_reply or {}).get("Mikrotik-Address-List"),
        "last_actuated_at": s.last_actuated_at,
    }


@router.get("/subscribers/by-username/{username}")
async def get_by_username(username: str, ctx: AdminCtx = Depends(admin_ctx)):
    sub = (await ctx.db.execute(
        select(Subscriber).where(Subscriber.username == username, Subscriber.tenant_id == ctx.tenant_id)
    )).scalar_one_or_none()
    if sub is None:
        raise HTTPException(404, "subscriber not found")
    return await _detail(ctx, sub)


@router.get("/subscribers/{subscriber_id}")
async def get_subscriber(subscriber_id: uuid.UUID, ctx: AdminCtx = Depends(admin_ctx)):
    return await _detail(ctx, await _get_sub(ctx, subscriber_id))


@router.patch("/subscribers/{subscriber_id}")
async def update_subscriber(subscriber_id: uuid.UUID, body: SubscriberPatch, ctx: AdminCtx = Depends(admin_ctx)):
    sub = await _get_sub(ctx, subscriber_id)
    data = _sub_values(body.model_dump(exclude_unset=True))
    for flag in ("enabled", "exempt_time_of_day", "exempt_fup"):
        if flag in data and data[flag] is None:
            raise HTTPException(422, f"{flag} cannot be null")
    if "custom_attributes" in data and data["custom_attributes"] is None:
        data["custom_attributes"] = []
    old, new = changes(sub, data)
    for k, v in data.items():
        setattr(sub, k, v)
    ctx.audit("subscriber.update", f"subscriber:{sub.id}", old, new)
    await ctx.db.commit()
    await enqueue(ctx.redis, sub.id)
    return await _detail(ctx, sub)


@router.delete("/subscribers/{subscriber_id}", status_code=204)
async def delete_subscriber(subscriber_id: uuid.UUID, ctx: AdminCtx = Depends(admin_ctx),
                            sender: CoASender = Depends(get_sender)):
    sub = await _get_sub(ctx, subscriber_id)
    has_billing = (await ctx.db.execute(select(func.count()).select_from(Invoice).where(Invoice.subscriber_id == sub.id))).scalar_one()
    if has_billing:
        raise HTTPException(409, "subscriber has billing history; disable instead of deleting")
    results = await disconnect_subscriber(ctx.db, ctx.redis, sender, sub.id, ctx.now, ctx.actor, "subscriber deleted")
    for model in (Credential, QuotaState, GateStatus, Balance, UsageDaily, AddonSubscription,
                  SubscriberPolicyException, RadiusSession, Subscription):
        await ctx.db.execute(delete(model).where(model.subscriber_id == sub.id))
    ctx.audit("subscriber.delete", f"subscriber:{sub.id}", {"username": sub.username},
              {"disconnects": [r.as_dict() for r in results]})
    await ctx.db.delete(sub)
    await ctx.db.commit()


# --- actions -----------------------------------------------------------------

async def _set_enabled(ctx: AdminCtx, sub: Subscriber, enabled: bool) -> None:
    if sub.enabled != enabled:
        ctx.audit("subscriber.enable" if enabled else "subscriber.disable", f"subscriber:{sub.id}",
                  {"enabled": sub.enabled}, {"enabled": enabled})
        sub.enabled = enabled


@router.post("/subscribers/{subscriber_id}/enable")
async def enable(subscriber_id: uuid.UUID, ctx: AdminCtx = Depends(admin_ctx)):
    sub = await _get_sub(ctx, subscriber_id)
    await _set_enabled(ctx, sub, True)
    await ctx.db.commit()
    return await _detail(ctx, sub)


@router.post("/subscribers/{subscriber_id}/disable")
async def disable(subscriber_id: uuid.UUID, ctx: AdminCtx = Depends(admin_ctx),
                  sender: CoASender = Depends(get_sender)):
    sub = await _get_sub(ctx, subscriber_id)
    await _set_enabled(ctx, sub, False)
    await ctx.db.flush()
    results = await reconcile_subscriber(ctx.db, ctx.redis, sender, sub.id, ctx.now, ctx.actor, "disabled")
    await ctx.db.commit()
    return {**await _detail(ctx, sub), "actuation": [r.as_dict() for r in results]}


@router.post("/subscribers/{subscriber_id}/password")
async def set_password(subscriber_id: uuid.UUID, body: PasswordIn, ctx: AdminCtx = Depends(admin_ctx)):
    sub = await _get_sub(ctx, subscriber_id)
    if not body.password and not body.generate:
        raise HTTPException(422, "password or generate is required")
    password = body.password or _generate_password()
    cred = await ctx.db.get(Credential, sub.id)
    if cred is None:
        ctx.db.add(Credential(subscriber_id=sub.id, password_hash=hash_password(password), rotated_at=ctx.now))
    else:
        cred.password_hash = hash_password(password)
        cred.forced_rotation = False
        cred.rotated_at = ctx.now
    ctx.audit("subscriber.password", f"subscriber:{sub.id}", None, {"generated": body.generate})
    await ctx.db.commit()
    return {"ok": True, **({"generated_password": password} if body.generate else {})}


@router.post("/subscribers/{subscriber_id}/renew")
async def renew_subscriber(subscriber_id: uuid.UUID, body: RenewIn, ctx: AdminCtx = Depends(admin_ctx)):
    sub = await _get_sub(ctx, subscriber_id)
    subscription = await _subscription(ctx.db, sub.id)
    plan = await ctx.db.get(Plan, subscription.plan_id)
    before = subscription.expiration
    new_exp = await renew(ctx.db, subscription, plan, ctx.now, body.days, body.reset_quota)
    ctx.audit("subscriber.renew", f"subscriber:{sub.id}", jsonable({"expiration": before}),
              jsonable({"expiration": new_exp, "days": body.days or plan.validity_days, "reset_quota": body.reset_quota}))
    await ctx.db.commit()
    await enqueue(ctx.redis, sub.id)
    return await _detail(ctx, sub)


@router.post("/subscribers/{subscriber_id}/change-profile")
async def change_profile(subscriber_id: uuid.UUID, body: ChangeProfileIn, ctx: AdminCtx = Depends(admin_ctx)):
    sub = await _get_sub(ctx, subscriber_id)
    subscription = await _subscription(ctx.db, sub.id)
    new_plan = await _get_plan(ctx, body.profile_id)
    old_plan_id = subscription.plan_id
    if body.when == "now":
        await change_plan(ctx.db, subscription, new_plan, ctx.now, reset_quota=body.reset_quota)
        detail = {"plan_id": new_plan.id, "when": "now"}
    else:
        at = body.at if body.when == "at" else subscription.expiration
        if at is None:
            raise HTTPException(422, "no date to schedule the change at")
        subscription.next_profile_change_plan_id = new_plan.id
        subscription.next_profile_change_at = at
        detail = {"plan_id": new_plan.id, "when": body.when, "at": at}
    ctx.audit("subscriber.change_profile", f"subscriber:{sub.id}", jsonable({"plan_id": old_plan_id}), jsonable(detail))
    await ctx.db.commit()
    await enqueue(ctx.redis, sub.id)
    return await _detail(ctx, sub)


@router.delete("/subscribers/{subscriber_id}/scheduled-change")
async def cancel_scheduled_change(subscriber_id: uuid.UUID, ctx: AdminCtx = Depends(admin_ctx)):
    sub = await _get_sub(ctx, subscriber_id)
    subscription = await _subscription(ctx.db, sub.id)
    subscription.next_profile_change_plan_id = None
    subscription.next_profile_change_at = None
    ctx.audit("subscriber.cancel_scheduled_change", f"subscriber:{sub.id}")
    await ctx.db.commit()
    return await _detail(ctx, sub)


@router.post("/subscribers/{subscriber_id}/quota/reset")
async def reset_quota(subscriber_id: uuid.UUID, body: QuotaResetIn, ctx: AdminCtx = Depends(admin_ctx)):
    sub = await _get_sub(ctx, subscriber_id)
    subscription = await _subscription(ctx.db, sub.id)
    plan = await ctx.db.get(Plan, subscription.plan_id)
    periods = {"daily": (QuotaPeriod.DAILY,), "monthly": (QuotaPeriod.MONTHLY,),
               "all": (QuotaPeriod.MONTHLY, QuotaPeriod.DAILY)}[body.period]
    await provision_quotas(ctx.db, subscription, plan, ctx.now, periods)
    if body.period in ("daily", "all"):
        # The daily policy reads today's counted usage; zero it so FUP lifts too.
        row = await ctx.db.get(UsageDaily, (sub.id, policy_day(ctx.now)))
        if row is not None:
            row.counted_rx_bytes = row.counted_tx_bytes = 0
    ctx.audit("subscriber.quota_reset", f"subscriber:{sub.id}", None, {"period": body.period})
    await ctx.db.commit()
    await enqueue(ctx.redis, sub.id)
    return await _detail(ctx, sub)


@router.post("/subscribers/{subscriber_id}/quota/topup")
async def topup(subscriber_id: uuid.UUID, body: TopUpIn, ctx: AdminCtx = Depends(admin_ctx)):
    sub = await _get_sub(ctx, subscriber_id)
    kind, period = QuotaKind(body.kind), QuotaPeriod(body.period)
    row = await ctx.db.get(QuotaState, (sub.id, kind, period))
    if row is None:
        raise HTTPException(409, f"profile has no {body.period} {body.kind} quota to top up (unlimited)")
    before = row.remaining
    row.remaining += body.bytes
    row.updated_at = ctx.now
    ctx.audit("subscriber.quota_topup", f"subscriber:{sub.id}", {"remaining": before},
              {"remaining": row.remaining, "added": body.bytes, "kind": body.kind, "period": body.period})
    await ctx.db.commit()
    await enqueue(ctx.redis, sub.id)
    return await _detail(ctx, sub)


@router.post("/subscribers/{subscriber_id}/mac")
async def mac_binding(subscriber_id: uuid.UUID, body: MacIn, ctx: AdminCtx = Depends(admin_ctx)):
    sub = await _get_sub(ctx, subscriber_id)
    before = sub.mac_binding
    if body.action == "clear":
        sub.mac_binding = None
    elif body.action == "set":
        if not body.mac:
            raise HTTPException(422, "mac is required for action=set")
        sub.mac_binding = body.mac
    else:
        sessions = await live_sessions(ctx.db, sub.id, ctx.now)
        mac = next((s.calling_station_id for s in sessions if s.calling_station_id), None)
        if mac is None:
            last = (await ctx.db.execute(
                select(RadiusSession.calling_station_id).where(
                    RadiusSession.subscriber_id == sub.id, RadiusSession.calling_station_id.is_not(None)
                ).order_by(RadiusSession.updated_at.desc()).limit(1)
            )).scalar_one_or_none()
            mac = last
        if mac is None:
            raise HTTPException(409, "no session with a Calling-Station-Id seen for this subscriber")
        sub.mac_binding = normalize_mac(mac)
    ctx.audit("subscriber.mac", f"subscriber:{sub.id}", {"mac_binding": before}, {"mac_binding": sub.mac_binding})
    await ctx.db.commit()
    return await _detail(ctx, sub)


@router.post("/subscribers/{subscriber_id}/disconnect")
async def disconnect(subscriber_id: uuid.UUID, ctx: AdminCtx = Depends(admin_ctx),
                     sender: CoASender = Depends(get_sender)):
    sub = await _get_sub(ctx, subscriber_id)
    results = await disconnect_subscriber(ctx.db, ctx.redis, sender, sub.id, ctx.now, ctx.actor)
    await ctx.db.commit()
    return {"results": [r.as_dict() for r in results]}


@router.post("/subscribers/{subscriber_id}/reconcile")
async def reconcile_now(subscriber_id: uuid.UUID, ctx: AdminCtx = Depends(admin_ctx),
                        sender: CoASender = Depends(get_sender)):
    sub = await _get_sub(ctx, subscriber_id)
    results = await reconcile_subscriber(ctx.db, ctx.redis, sender, sub.id, ctx.now, ctx.actor, "manual reconcile")
    await ctx.db.commit()
    return {"results": [r.as_dict() for r in results]}


# --- history -----------------------------------------------------------------

@router.get("/subscribers/{subscriber_id}/usage", response_model=list[UsageDay])
async def usage(subscriber_id: uuid.UUID, days: int = Query(default=30, ge=1, le=400),
                ctx: AdminCtx = Depends(admin_ctx)):
    sub = await _get_sub(ctx, subscriber_id)
    today = policy_day(ctx.now)
    first = today - timedelta(days=days - 1)
    rows = {r.day: r for r in (await ctx.db.execute(
        select(UsageDaily).where(UsageDaily.subscriber_id == sub.id, UsageDaily.day >= first)
    )).scalars().all()}
    out = []
    for i in range(days):
        d = first + timedelta(days=i)
        r = rows.get(d)
        out.append(UsageDay(day=d, rx_bytes=r.rx_bytes if r else 0, tx_bytes=r.tx_bytes if r else 0,
                            uptime_s=r.uptime_s if r else 0, counted_rx_bytes=r.counted_rx_bytes if r else 0,
                            counted_tx_bytes=r.counted_tx_bytes if r else 0))
    return out


@router.get("/subscribers/{subscriber_id}/sessions")
async def session_history(subscriber_id: uuid.UUID, limit: int = Query(default=50, ge=1, le=500),
                          ctx: AdminCtx = Depends(admin_ctx)):
    sub = await _get_sub(ctx, subscriber_id)
    rows = (await ctx.db.execute(
        select(RadiusSession).where(RadiusSession.subscriber_id == sub.id)
        .order_by(RadiusSession.started_at.desc()).limit(limit)
    )).scalars().all()
    return [_session_view(r) for r in rows]


@router.get("/subscribers/{subscriber_id}/auth-log")
async def auth_log(subscriber_id: uuid.UUID, limit: int = Query(default=50, ge=1, le=500),
                   ctx: AdminCtx = Depends(admin_ctx)):
    sub = await _get_sub(ctx, subscriber_id)
    rows = (await ctx.db.execute(
        select(AuthEvent).where(AuthEvent.username == sub.username).order_by(AuthEvent.ts.desc()).limit(limit)
    )).scalars().all()
    return [{"ts": r.ts, "result": r.result, "reason": r.reason, "nas_ip": r.nas_ip,
             "calling_station_id": r.calling_station_id} for r in rows]


# --- bulk --------------------------------------------------------------------

@router.post("/subscribers/bulk")
async def bulk(body: BulkIn, ctx: AdminCtx = Depends(admin_ctx), sender: CoASender = Depends(get_sender)):
    if body.action == "change_profile" and not body.profile_id:
        raise HTTPException(422, "profile_id is required for change_profile")
    new_plan = await _get_plan(ctx, body.profile_id) if body.action == "change_profile" else None
    done, missing = [], []
    for sid in body.ids:
        sub = await ctx.db.get(Subscriber, sid)
        if sub is None or sub.tenant_id != ctx.tenant_id:
            missing.append(str(sid))
            continue
        if body.action in ("enable", "disable"):
            await _set_enabled(ctx, sub, body.action == "enable")
        elif body.action == "renew":
            subscription = await ctx.db.get(Subscription, sub.id)
            if subscription is None:
                missing.append(str(sid))
                continue
            plan = await ctx.db.get(Plan, subscription.plan_id)
            new_exp = await renew(ctx.db, subscription, plan, ctx.now, body.days)
            ctx.audit("subscriber.renew", f"subscriber:{sub.id}", None,
                      jsonable({"expiration": new_exp, "bulk": True}))
        elif body.action == "change_profile":
            subscription = await ctx.db.get(Subscription, sub.id)
            if subscription is None:
                missing.append(str(sid))
                continue
            old = subscription.plan_id
            await change_plan(ctx.db, subscription, new_plan, ctx.now)
            ctx.audit("subscriber.change_profile", f"subscriber:{sub.id}", jsonable({"plan_id": old}),
                      jsonable({"plan_id": new_plan.id, "bulk": True}))
        done.append(sub.id)
    await ctx.db.flush()
    if body.action == "disconnect":
        for sid in done:
            await disconnect_subscriber(ctx.db, ctx.redis, sender, sid, ctx.now, ctx.actor, "bulk disconnect")
    await ctx.db.commit()
    if body.action != "disconnect":
        await enqueue(ctx.redis, *done)
    return {"done": len(done), "missing": missing}
