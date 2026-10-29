import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from ipaddress import IPv4Address

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.plan import Subscription
from app.models.session import RadiusSession


def jsonable(value):
    """Audit-log-safe copy: UUIDs, decimals, IPs, datetimes to strings."""
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, (uuid.UUID, IPv4Address, Decimal)):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def changes(obj, data: dict) -> tuple[dict, dict]:
    old, new = {}, {}
    for k, v in data.items():
        before = getattr(obj, k, None)
        if jsonable(before) != jsonable(v):
            old[k], new[k] = jsonable(before), jsonable(v)
    return old, new


async def live_subscribers_on_plans(db: AsyncSession, plan_ids: set, now: datetime) -> set:
    if not plan_ids:
        return set()
    q = (
        select(RadiusSession.subscriber_id)
        .join(Subscription, Subscription.subscriber_id == RadiusSession.subscriber_id)
        .where(
            Subscription.plan_id.in_(plan_ids),
            RadiusSession.stopped_at.is_(None),
            RadiusSession.updated_at > now - timedelta(seconds=settings.session_stale_after_s),
        )
        .distinct()
    )
    return set((await db.execute(q)).scalars().all())
