"""Turns a RADIUS accounting packet into a quota decrement. The one piece of
real complexity: RADIUS Interim-Update/Stop packets report session-cumulative
octets, not deltas, so this has to diff against the last-seen cumulative
value per session (RadiusSession) to get the incremental usage the quota
engine's pure apply_usage() actually wants.
"""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clickhouse_client import insert_radacct_event
from app.models.quota import QuotaState
from app.models.session import RadiusSession
from app.models.subscriber import Subscriber
from app.policy.quota_engine import (
    AccountingDelta,
    QuotaKind,
    QuotaLedger,
    QuotaPeriod,
    apply_usage,
    quota_field_name,
)

_ALL_KEYS = [(k, p) for k in QuotaKind for p in QuotaPeriod]


async def record_accounting(
    db: AsyncSession,
    username: str,
    acct_session_id: str,
    acct_status_type: str,
    nas_ip: str,
    acct_session_time: int,
    acct_input_octets: int,
    acct_output_octets: int,
    acct_terminate_cause: str | None,
    now: datetime,
) -> None:
    subscriber = (
        await db.execute(select(Subscriber).where(Subscriber.username == username))
    ).scalar_one_or_none()
    if subscriber is None:
        return  # unknown username in an accounting packet: nothing to decrement against

    session_row = (
        await db.execute(select(RadiusSession).where(RadiusSession.acct_session_id == acct_session_id))
    ).scalar_one_or_none()

    if session_row is None:
        # last_input_octets/last_output_octets/last_session_time_s are passed
        # explicitly rather than left to their column default=0: a column
        # default only applies at flush/INSERT time, but the delta
        # computation just below reads these attributes on the in-memory
        # object immediately after construction, before any flush happens.
        session_row = RadiusSession(
            acct_session_id=acct_session_id,
            subscriber_id=subscriber.id,
            nas_ip=nas_ip,
            last_input_octets=0,
            last_output_octets=0,
            last_session_time_s=0,
            updated_at=now,
        )
        db.add(session_row)

    delta = AccountingDelta(
        rx_bytes=max(0, acct_input_octets - session_row.last_input_octets),
        tx_bytes=max(0, acct_output_octets - session_row.last_output_octets),
        session_seconds=max(0, acct_session_time - session_row.last_session_time_s),
    )

    session_row.last_input_octets = acct_input_octets
    session_row.last_output_octets = acct_output_octets
    session_row.last_session_time_s = acct_session_time
    session_row.updated_at = now
    if acct_status_type == "Stop":
        session_row.stopped_at = now

    quota_rows = (
        await db.execute(select(QuotaState).where(QuotaState.subscriber_id == subscriber.id))
    ).scalars().all()
    quota_by_key = {(row.kind, row.period): row for row in quota_rows}

    ledger = QuotaLedger(**{
        quota_field_name(k, p): (quota_by_key[(k, p)].remaining if (k, p) in quota_by_key else None)
        for k, p in _ALL_KEYS
    })
    updated = apply_usage(ledger, delta)

    for k, p in _ALL_KEYS:
        row = quota_by_key.get((k, p))
        if row is None:
            continue  # no quota row provisioned for this kind/period: unlimited, nothing to write
        new_value = getattr(updated, quota_field_name(k, p))
        if new_value is not None:
            row.remaining = new_value
            row.updated_at = now

    insert_radacct_event(
        event_ts=now,
        subscriber_id=str(subscriber.id),
        username=username,
        nas_ip=nas_ip,
        acct_session_id=acct_session_id,
        status_type=acct_status_type,
        session_time_s=acct_session_time,
        input_octets=acct_input_octets,
        output_octets=acct_output_octets,
        terminate_cause=acct_terminate_cause or "",
    )
