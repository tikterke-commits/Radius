"""Endpoints FreeRADIUS's rlm_rest module calls for authorize / authenticate /
accounting / post-auth, per A1. The response contract (status code drives the
module rcode, reply:/control:-prefixed JSON keys drive attribute lists) is
rlm_rest's real behavior, verified against the module's own config comments —
see app/schemas/radius.py's docstring and radius/mods-enabled/rest.
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Response
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_db
from app.models.credential import Credential
from app.models.subscriber import Subscriber
from app.policy.accounting_service import record_accounting
from app.policy.authorize_service import compute_authorize_decision
from app.policy.decision_cache import RedisDecisionCache, authorize_with_budget
from app.redis_client import get_redis
from app.schemas.radius import (
    AccountingRequest,
    AuthenticateRequest,
    AuthorizeRequest,
    PostAuthRequest,
)
from app.security.credentials import verify_password

router = APIRouter(prefix="/radius", tags=["radius"])


@router.post("/authorize")
async def authorize(req: AuthorizeRequest, db: AsyncSession = Depends(get_db)) -> JSONResponse:
    cache = RedisDecisionCache(get_redis())
    now = datetime.now(timezone.utc)

    async def compute():
        return await compute_authorize_decision(db, req.username, req.calling_station_id, now)

    decision = await authorize_with_budget(
        username=req.username,
        compute=compute,
        cache=cache,
        timeout_s=settings.authz_backend_timeout_ms / 1000,
        fail_mode=settings.authz_fail_mode,
        cache_ttl_s=settings.authz_decision_cache_ttl_s,
    )

    if decision.accepted:
        reply_attributes = decision.reply_attributes
        if decision.reason == "fail_mode:open" and "Mikrotik-Rate-Limit" not in reply_attributes:
            rate = settings.authz_fail_open_default_rate_bps
            reply_attributes = {**reply_attributes, "Mikrotik-Rate-Limit": f"{rate}/{rate}"}
        body = {f"reply:{k}": v for k, v in reply_attributes.items()}
        return JSONResponse(status_code=200, content=body)

    # 401: rlm_rest still processes the body for a rejected authorize/authenticate
    # call, so the reason travels with the reject as a Reply-Message the NAS
    # can surface (and the platform's audit log / support tooling can read
    # back from RADIUS logs).
    return JSONResponse(status_code=401, content={"reply:Reply-Message": decision.reason})


@router.post("/authenticate")
async def authenticate(req: AuthenticateRequest, db: AsyncSession = Depends(get_db)) -> JSONResponse:
    """PAP only, per the A5 decision (docs/00-part-h-design.md §5) — no
    MS-CHAPv2/CHAP support, so this endpoint only ever sees a cleartext
    password to check against the stored Argon2id hash. It is never logged.
    """
    subscriber = (
        await db.execute(select(Subscriber).where(Subscriber.username == req.username))
    ).scalar_one_or_none()
    if subscriber is None or not subscriber.enabled:
        return JSONResponse(status_code=401, content={"reply:Reply-Message": "no_such_subscriber"})

    credential = (
        await db.execute(select(Credential).where(Credential.subscriber_id == subscriber.id))
    ).scalar_one_or_none()
    if credential is None or not verify_password(req.password, credential.password_hash):
        return JSONResponse(status_code=401, content={"reply:Reply-Message": "bad_credentials"})

    return JSONResponse(status_code=200, content={})


@router.post("/accounting", status_code=204)
async def accounting(req: AccountingRequest, db: AsyncSession = Depends(get_db)) -> Response:
    now = datetime.now(timezone.utc)
    await record_accounting(
        db=db,
        username=req.username,
        acct_session_id=req.acct_session_id,
        acct_status_type=req.acct_status_type,
        nas_ip=req.nas_ip_address,
        acct_session_time=req.acct_session_time,
        acct_input_octets=req.acct_input_octets,
        acct_output_octets=req.acct_output_octets,
        acct_terminate_cause=req.acct_terminate_cause,
        now=now,
    )
    await db.commit()
    return Response(status_code=204)


@router.post("/post-auth", status_code=204)
async def post_auth(req: PostAuthRequest) -> Response:
    # Placeholder hook for post-auth side effects (audit log entry, event-bus
    # emit for notifications per A8). Deliberately not on the authorize hot
    # path itself — post-auth runs after FreeRADIUS has already decided.
    return Response(status_code=204)
