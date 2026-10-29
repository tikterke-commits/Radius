"""Reconcile, not delta: for each live session, compute what it *should* have
right now (the same decide() authorize uses), diff against what it was last
given, and close the gap with the cheapest correct action:

  - nothing differs            -> no-op
  - only CoA-mutable attrs     -> CoA-Request (falls back to Disconnect on NAK)
  - anything else / rejected   -> Disconnect-Request; the CPE redials and the
                                  new Access-Accept carries the new attributes

Safety rails, matching Atlas doctrine: dry-run by default (log the plan,
send nothing), a per-minute change ceiling, and a panic switch that stops
all actuation. Every action — dry-run included — is an audit_log row with
the NAS's response.
"""

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.actuation.coa import CoAResult, CoASender
from app.config import settings
from app.models.audit import AuditLog
from app.models.nas import NasDevice
from app.models.session import RadiusSession
from app.models.subscriber import Subscriber
from app.policy.authorize_service import decision_for_subscriber
from app.policy.decision import stable_attributes
from app.security.secrets import SecretKeyMissing, decrypt_secret

logger = logging.getLogger(__name__)

PENDING_KEY = "reconcile:pending"
PANIC_KEY = "actuation:panic"


@dataclass
class ActionResult:
    acct_session_id: str
    action: str  # noop | coa | disconnect | skipped
    detail: str
    response: dict | None = None

    def as_dict(self) -> dict:
        return {"acct_session_id": self.acct_session_id, "action": self.action,
                "detail": self.detail, "response": self.response}


# --- queue ---------------------------------------------------------------

async def enqueue(redis, *subscriber_ids) -> None:
    ids = [str(s) for s in subscriber_ids if s]
    if ids:
        await redis.sadd(PENDING_KEY, *ids)


async def drain(redis, limit: int = 200) -> list[str]:
    popped = await redis.spop(PENDING_KEY, limit)
    return list(popped or [])


async def panic_on(redis) -> bool:
    return bool(await redis.get(PANIC_KEY))


async def set_panic(redis, on: bool) -> None:
    if on:
        await redis.set(PANIC_KEY, "1")
    else:
        await redis.delete(PANIC_KEY)


async def _within_ceiling(redis, now: datetime) -> bool:
    key = f"actuation:count:{now:%Y%m%d%H%M}"
    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, 120)
    return count <= settings.actuation_max_per_minute


# --- helpers -------------------------------------------------------------

async def live_sessions(db: AsyncSession, subscriber_id, now: datetime) -> list[RadiusSession]:
    return list((
        await db.execute(
            select(RadiusSession).where(
                RadiusSession.subscriber_id == subscriber_id,
                RadiusSession.stopped_at.is_(None),
                RadiusSession.updated_at > now - timedelta(seconds=settings.session_stale_after_s),
            )
        )
    ).scalars().all())


def _session_keys(session: RadiusSession) -> dict:
    keys = {"User-Name": session.username, "Acct-Session-Id": session.acct_session_id}
    if session.framed_ip:
        keys["Framed-IP-Address"] = session.framed_ip
    return keys


def _audit(db, tenant_id, actor, action, subject, old, new, nas_response) -> None:
    db.add(AuditLog(tenant_id=tenant_id, actor=actor, action=action, subject=subject,
                    old_value=old, new_value=new, nas_response=nas_response))


async def _nas_for(db: AsyncSession, nas_ip: str) -> NasDevice | None:
    return (await db.execute(select(NasDevice).where(NasDevice.ip == nas_ip))).scalar_one_or_none()


async def _dry_run_is_new(redis, session_id: str, plan: dict) -> bool:
    digest = hashlib.sha256(json.dumps(plan, sort_keys=True, default=str).encode()).hexdigest()
    key = f"dryrun:{session_id}"
    if await redis.get(key) == digest:
        return False
    await redis.set(key, digest, ex=3600)
    return True


# --- core ----------------------------------------------------------------

async def _act_on_session(
    db: AsyncSession, redis, sender: CoASender, subscriber: Subscriber, session: RadiusSession,
    desired: dict | None, now: datetime, actor: str, reason: str, force_disconnect: bool = False,
) -> ActionResult:
    sid = session.acct_session_id
    subject = f"subscriber:{subscriber.id}"
    nas = await _nas_for(db, session.nas_ip)

    if desired is None or force_disconnect:
        action, attrs = "disconnect", {}
    else:
        want, have = stable_attributes(desired), stable_attributes(session.last_reply)
        mutable = set(settings.coa_mutable_attributes)
        if session.last_reply is None:
            # Baseline unknown (the Access-Accept wasn't captured): push the
            # mutable subset, assume the rest arrived with the Accept.
            changed = {k for k in want if k in mutable}
        else:
            changed = {k for k in set(want) | set(have) if want.get(k) != have.get(k)}
        if not changed:
            return ActionResult(sid, "noop", "already in sync")
        nas_caps = (nas.capability_matrix or {}) if nas is not None else {}
        coa_ok_on_nas = nas_caps.get("coa_rate_change_applies_midsession", True)
        removed = {k for k in changed if k not in want}
        if changed <= mutable and not removed and coa_ok_on_nas:
            action, attrs = "coa", {k: want[k] for k in sorted(changed)}
        else:
            action, attrs = "disconnect", {}

    if nas is None or not nas.enabled:
        _audit(db, subscriber.tenant_id, actor, "actuation.skipped", subject,
               None, {"session": sid, "planned": action, "reason": reason},
               {"error": "nas_unknown_or_disabled", "nas_ip": session.nas_ip})
        return ActionResult(sid, "skipped", f"NAS {session.nas_ip} not registered or disabled")

    planned = {"session": sid, "action": action, "attributes": attrs, "reason": reason}

    if await panic_on(redis):
        return ActionResult(sid, "skipped", "panic switch on")

    if settings.actuation_dry_run:
        if await _dry_run_is_new(redis, sid, planned):
            _audit(db, subscriber.tenant_id, actor, f"actuation.{action}", subject,
                   session.last_reply, planned, {"dry_run": True})
        return ActionResult(sid, "skipped", f"dry-run: would {action}", {"dry_run": True})

    if not await _within_ceiling(redis, now):
        await enqueue(redis, subscriber.id)
        return ActionResult(sid, "skipped", "per-minute actuation ceiling reached; requeued")

    try:
        secret = decrypt_secret(nas.secret_encrypted)
    except SecretKeyMissing as exc:
        _audit(db, subscriber.tenant_id, actor, "actuation.skipped", subject, None, planned, {"error": str(exc)})
        return ActionResult(sid, "skipped", str(exc))

    keys = _session_keys(session)
    if action == "coa":
        result: CoAResult = await sender.coa(str(nas.ip), nas.coa_port, secret, {**keys, **attrs})
        _audit(db, subscriber.tenant_id, actor, "actuation.coa", subject, session.last_reply, planned, result.as_dict())
        if result.ok:
            session.last_reply = desired
            session.last_actuated_at = now
            return ActionResult(sid, "coa", reason, result.as_dict())
        # NAK or timeout: the session still has stale attributes — redial it.
        action = "disconnect"

    result = await sender.disconnect(str(nas.ip), nas.coa_port, secret, keys)
    _audit(db, subscriber.tenant_id, actor, "actuation.disconnect", subject, session.last_reply,
           {**planned, "action": "disconnect"}, result.as_dict())
    if result.ok:
        session.last_actuated_at = now
    return ActionResult(sid, "disconnect" if result.ok else "skipped",
                        reason if result.ok else f"disconnect failed: {result.response}", result.as_dict())


async def reconcile_subscriber(
    db: AsyncSession, redis, sender: CoASender, subscriber_id, now: datetime,
    actor: str = "system:reconcile", reason: str = "reconcile",
) -> list[ActionResult]:
    subscriber = await db.get(Subscriber, subscriber_id)
    if subscriber is None:
        return []
    sessions = await live_sessions(db, subscriber.id, now)
    if not sessions:
        return []
    decision = await decision_for_subscriber(db, subscriber, now, check_concurrency=False)
    desired = decision.reply_attributes if decision.accepted else None
    results = []
    for session in sessions:
        results.append(await _act_on_session(
            db, redis, sender, subscriber, session, desired, now, actor,
            reason if decision.accepted else f"{reason}:{decision.reason}",
        ))
    return results


async def disconnect_subscriber(
    db: AsyncSession, redis, sender: CoASender, subscriber_id, now: datetime,
    actor: str, reason: str = "admin disconnect", acct_session_id: str | None = None,
) -> list[ActionResult]:
    subscriber = await db.get(Subscriber, subscriber_id)
    if subscriber is None:
        return []
    sessions = await live_sessions(db, subscriber.id, now)
    if acct_session_id:
        sessions = [s for s in sessions if s.acct_session_id == acct_session_id]
    return [
        await _act_on_session(db, redis, sender, subscriber, s, None, now, actor, reason, force_disconnect=True)
        for s in sessions
    ]
