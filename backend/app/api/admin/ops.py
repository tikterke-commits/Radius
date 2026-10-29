"""NAS inventory, live sessions, auth log, audit, dashboard stats, actuation
controls."""

import uuid
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select

from app.actuation.coa import CoASender
from app.actuation.service import PENDING_KEY, disconnect_subscriber, panic_on, set_panic
from app.api.admin.deps import AdminCtx, admin_ctx, get_sender
from app.api.admin.subscribers import _session_view
from app.api.admin.util import changes, jsonable
from app.config import settings
from app.models.audit import AuditLog
from app.models.nas import NasDevice
from app.models.plan import Plan, Subscription
from app.models.session import RadiusSession
from app.models.subscriber import Subscriber
from app.models.usage import AuthEvent, UsageDaily
from app.policy.clock import policy_day
from app.schemas.admin import NasIn, NasOut, NasPatch, PanicIn
from app.security.secrets import SecretKeyMissing, encrypt_secret
from app.services.nas_clients import write_clients_file

router = APIRouter()


def _live(now):
    return (
        RadiusSession.stopped_at.is_(None),
        RadiusSession.updated_at > now - timedelta(seconds=settings.session_stale_after_s),
    )


# --- NAS ---------------------------------------------------------------------

async def _nas_out(ctx: AdminCtx, nas: NasDevice) -> NasOut:
    out = NasOut.model_validate(nas)
    out.online_sessions = (await ctx.db.execute(
        select(func.count()).select_from(RadiusSession).where(RadiusSession.nas_ip == str(nas.ip), *_live(ctx.now))
    )).scalar_one()
    return out


async def _get_nas(ctx: AdminCtx, nas_id: uuid.UUID) -> NasDevice:
    nas = await ctx.db.get(NasDevice, nas_id)
    if nas is None or nas.tenant_id != ctx.tenant_id:
        raise HTTPException(404, "NAS not found")
    return nas


async def _sync_clients(ctx: AdminCtx) -> dict:
    try:
        return await write_clients_file(ctx.db)
    except SecretKeyMissing as exc:
        return {"written": False, "error": str(exc)}


@router.get("/nas", response_model=list[NasOut])
async def list_nas(ctx: AdminCtx = Depends(admin_ctx)):
    rows = (await ctx.db.execute(select(NasDevice).where(NasDevice.tenant_id == ctx.tenant_id).order_by(NasDevice.name))).scalars().all()
    return [await _nas_out(ctx, n) for n in rows]


@router.post("/nas", status_code=201)
async def create_nas(body: NasIn, ctx: AdminCtx = Depends(admin_ctx)):
    if (await ctx.db.execute(select(NasDevice.id).where(NasDevice.ip == str(body.ip)))).first():
        raise HTTPException(409, "a NAS with this IP already exists")
    try:
        secret = encrypt_secret(body.secret)
    except SecretKeyMissing as exc:
        raise HTTPException(503, str(exc))
    data = body.model_dump(exclude={"secret"})
    data["ip"] = str(data["ip"])
    nas = NasDevice(tenant_id=ctx.tenant_id, secret_encrypted=secret, **data)
    ctx.db.add(nas)
    await ctx.db.flush()
    ctx.audit("nas.create", f"nas:{nas.id}", None, jsonable(data))
    await ctx.db.commit()
    return {"nas": await _nas_out(ctx, nas), "clients_file": await _sync_clients(ctx)}


@router.patch("/nas/{nas_id}")
async def update_nas(nas_id: uuid.UUID, body: NasPatch, ctx: AdminCtx = Depends(admin_ctx)):
    nas = await _get_nas(ctx, nas_id)
    data = body.model_dump(exclude_unset=True)
    for required in ("name", "ip", "ros_version", "coa_port", "enabled"):
        if required in data and data[required] is None:
            raise HTTPException(422, f"{required} cannot be null")
    secret = data.pop("secret", None)
    if "ip" in data:
        data["ip"] = str(data["ip"])
    if "capability_matrix" in data and data["capability_matrix"] is None:
        data["capability_matrix"] = {}
    old, new = changes(nas, data)
    for k, v in data.items():
        setattr(nas, k, v)
    if secret:
        try:
            nas.secret_encrypted = encrypt_secret(secret)
        except SecretKeyMissing as exc:
            raise HTTPException(503, str(exc))
        new["secret"] = "changed"
    ctx.audit("nas.update", f"nas:{nas.id}", old, new)
    await ctx.db.commit()
    return {"nas": await _nas_out(ctx, nas), "clients_file": await _sync_clients(ctx)}


@router.delete("/nas/{nas_id}")
async def delete_nas(nas_id: uuid.UUID, ctx: AdminCtx = Depends(admin_ctx)):
    nas = await _get_nas(ctx, nas_id)
    ctx.audit("nas.delete", f"nas:{nas.id}", {"name": nas.name, "ip": str(nas.ip)}, None)
    await ctx.db.delete(nas)
    await ctx.db.commit()
    return {"deleted": True, "clients_file": await _sync_clients(ctx)}


@router.post("/nas/sync-clients")
async def sync_clients(ctx: AdminCtx = Depends(admin_ctx)):
    """Re-render the FreeRADIUS clients file (metadata only — never the secrets)."""
    return await _sync_clients(ctx)


# --- sessions ----------------------------------------------------------------

@router.get("/sessions/online")
async def online_sessions(
    nas_ip: str | None = None, q: str | None = None,
    limit: int = Query(default=500, ge=1, le=5000), ctx: AdminCtx = Depends(admin_ctx),
):
    stmt = (
        select(RadiusSession, Subscriber.id, Plan.name)
        .join(Subscriber, Subscriber.id == RadiusSession.subscriber_id)
        .outerjoin(Subscription, Subscription.subscriber_id == Subscriber.id)
        .outerjoin(Plan, Plan.id == Subscription.plan_id)
        .where(Subscriber.tenant_id == ctx.tenant_id, *_live(ctx.now))
        .order_by(RadiusSession.username)
        .limit(limit)
    )
    if nas_ip:
        stmt = stmt.where(RadiusSession.nas_ip == nas_ip)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(RadiusSession.username.ilike(like) | RadiusSession.framed_ip.ilike(like)
                          | RadiusSession.calling_station_id.ilike(like))
    rows = (await ctx.db.execute(stmt)).all()
    return [{**_session_view(s), "subscriber_id": sid, "profile_name": pname} for s, sid, pname in rows]


@router.post("/sessions/{acct_session_id}/disconnect")
async def disconnect_session(acct_session_id: str, ctx: AdminCtx = Depends(admin_ctx),
                             sender: CoASender = Depends(get_sender)):
    session = await ctx.db.get(RadiusSession, acct_session_id)
    if session is None:
        raise HTTPException(404, "session not found")
    sub = await ctx.db.get(Subscriber, session.subscriber_id)
    if sub is None or sub.tenant_id != ctx.tenant_id:
        raise HTTPException(404, "session not found")
    results = await disconnect_subscriber(ctx.db, ctx.redis, sender, sub.id, ctx.now, ctx.actor,
                                          acct_session_id=acct_session_id)
    await ctx.db.commit()
    return {"results": [r.as_dict() for r in results]}


# --- logs --------------------------------------------------------------------

@router.get("/auth-log")
async def global_auth_log(
    result: str | None = Query(default=None, pattern="^(accept|reject)$"),
    username: str | None = None,
    limit: int = Query(default=100, ge=1, le=1000), ctx: AdminCtx = Depends(admin_ctx),
):
    stmt = select(AuthEvent).order_by(AuthEvent.ts.desc()).limit(limit)
    if result:
        stmt = stmt.where(AuthEvent.result == result)
    if username:
        stmt = stmt.where(AuthEvent.username == username)
    rows = (await ctx.db.execute(stmt)).scalars().all()
    return [{"ts": r.ts, "username": r.username, "result": r.result, "reason": r.reason,
             "nas_ip": r.nas_ip, "calling_station_id": r.calling_station_id} for r in rows]


@router.get("/audit")
async def audit(
    subject: str | None = None, action: str | None = None, actor: str | None = None,
    limit: int = Query(default=100, ge=1, le=1000), ctx: AdminCtx = Depends(admin_ctx),
):
    stmt = select(AuditLog).where(AuditLog.tenant_id == ctx.tenant_id).order_by(AuditLog.created_at.desc()).limit(limit)
    if subject:
        stmt = stmt.where(AuditLog.subject == subject)
    if action:
        stmt = stmt.where(AuditLog.action.startswith(action))
    if actor:
        stmt = stmt.where(AuditLog.actor == actor)
    rows = (await ctx.db.execute(stmt)).scalars().all()
    return [{"ts": r.created_at, "actor": r.actor, "action": r.action, "subject": r.subject,
             "old": r.old_value, "new": r.new_value, "nas_response": r.nas_response} for r in rows]


# --- stats / actuation -------------------------------------------------------

@router.get("/stats")
async def stats(ctx: AdminCtx = Depends(admin_ctx)):
    db, now = ctx.db, ctx.now
    base = select(func.count()).select_from(Subscriber).where(Subscriber.tenant_id == ctx.tenant_id)
    total = (await db.execute(base)).scalar_one()
    disabled = (await db.execute(base.where(Subscriber.enabled.is_(False)))).scalar_one()
    joined = (
        select(func.count()).select_from(Subscriber)
        .join(Subscription, Subscription.subscriber_id == Subscriber.id)
        .where(Subscriber.tenant_id == ctx.tenant_id, Subscriber.enabled.is_(True))
    )
    expired = (await db.execute(joined.where(Subscription.expiration <= now))).scalar_one()
    expiring_7d = (await db.execute(joined.where(Subscription.expiration > now,
                                                 Subscription.expiration <= now + timedelta(days=7)))).scalar_one()
    online = (await db.execute(
        select(func.count(func.distinct(RadiusSession.subscriber_id))).where(*_live(now))
    )).scalar_one()
    by_profile = (await db.execute(
        select(Plan.id, Plan.name, func.count(Subscription.subscriber_id))
        .outerjoin(Subscription, Subscription.plan_id == Plan.id)
        .where(Plan.tenant_id == ctx.tenant_id).group_by(Plan.id, Plan.name).order_by(Plan.name)
    )).all()
    today = (await db.execute(
        select(func.coalesce(func.sum(UsageDaily.rx_bytes), 0), func.coalesce(func.sum(UsageDaily.tx_bytes), 0))
        .where(UsageDaily.day == policy_day(now))
    )).one()
    rejects_1h = (await db.execute(
        select(func.count()).select_from(AuthEvent).where(AuthEvent.result == "reject",
                                                          AuthEvent.ts > now - timedelta(hours=1))
    )).scalar_one()
    return {
        "subscribers": {"total": total, "active": total - disabled - expired, "disabled": disabled,
                        "expired": expired, "expiring_7d": expiring_7d, "online": online},
        "by_profile": [{"profile_id": pid, "name": name, "subscribers": n} for pid, name, n in by_profile],
        "usage_today": {"rx_bytes": int(today[0]), "tx_bytes": int(today[1])},
        "auth_rejects_last_hour": rejects_1h,
        "actuation": await _actuation_status(ctx),
    }


async def _actuation_status(ctx: AdminCtx) -> dict:
    return {
        "dry_run": settings.actuation_dry_run,
        "panic": await panic_on(ctx.redis),
        "max_per_minute": settings.actuation_max_per_minute,
        "pending_reconcile": await ctx.redis.scard(PENDING_KEY),
        "coa_mutable_attributes": settings.coa_mutable_attributes,
    }


@router.get("/actuation")
async def actuation_status(ctx: AdminCtx = Depends(admin_ctx)):
    return await _actuation_status(ctx)


@router.post("/actuation/panic")
async def actuation_panic(body: PanicIn, ctx: AdminCtx = Depends(admin_ctx)):
    await set_panic(ctx.redis, body.on)
    ctx.audit("actuation.panic", "actuation", None, {"on": body.on})
    await ctx.db.commit()
    return await _actuation_status(ctx)
