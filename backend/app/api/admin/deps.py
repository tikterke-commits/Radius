"""Admin API plumbing: API-key auth, actor attribution, tenant resolution, and
the injectable CoA sender (tests swap in a fake NAS)."""

import hmac
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.actuation.coa import CoASender, PyradCoASender
from app.config import settings
from app.db import get_db
from app.models.audit import AuditLog
from app.models.tenant import Tenant
from app.redis_client import get_redis

_tenant_cache: dict[str, uuid.UUID] = {}


@dataclass
class AdminCtx:
    db: AsyncSession
    tenant_id: uuid.UUID
    actor: str
    redis: object
    now: datetime

    def audit(self, action: str, subject: str, old: dict | None = None, new: dict | None = None,
              nas_response: dict | None = None) -> None:
        self.db.add(AuditLog(tenant_id=self.tenant_id, actor=self.actor, action=action, subject=subject,
                             old_value=old, new_value=new, nas_response=nas_response))


async def _default_tenant(db: AsyncSession) -> uuid.UUID:
    name = settings.default_tenant_name
    cached = _tenant_cache.get(name)
    if cached is not None:
        if await db.get(Tenant, cached) is not None:
            return cached
        _tenant_cache.pop(name, None)
    tenant = (await db.execute(select(Tenant).where(Tenant.name == name, Tenant.parent_tenant_id.is_(None)))).scalars().first()
    if tenant is None:
        # Committed on its own: the first request may be a read-only GET
        # that never commits, which would roll the tenant back every time.
        tenant = Tenant(name=name)
        db.add(tenant)
        await db.commit()
    _tenant_cache[name] = tenant.id
    return tenant.id


async def admin_ctx(
    x_api_key: str | None = Header(default=None),
    x_actor: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> AdminCtx:
    if not settings.admin_api_key:
        raise HTTPException(503, "admin API disabled: ADMIN_API_KEY is not set")
    if not x_api_key or not hmac.compare_digest(x_api_key, settings.admin_api_key):
        raise HTTPException(401, "invalid API key")
    actor = (x_actor or "atlas")[:120]
    tenant_id = await _default_tenant(db)
    return AdminCtx(db=db, tenant_id=tenant_id, actor=f"atlas:{actor}" if not actor.startswith("atlas") else actor,
                    redis=get_redis(), now=datetime.now(timezone.utc))


_sender: CoASender | None = None


def get_sender() -> CoASender:
    global _sender
    if _sender is None:
        _sender = PyradCoASender(timeout_s=settings.coa_timeout_s, retries=settings.coa_retries)
    return _sender
