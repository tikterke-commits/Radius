"""Turns a RADIUS accounting packet into usage, quota decrements and session
state.

The real complexity:
  - Interim-Update/Stop report session-cumulative octets, not deltas, so the
    engine diffs against the last-seen cumulative value per session.
  - Acct-Input/Output-Octets are 32-bit and wrap at 4 GiB; the true total is
    gigawords << 32 | octets. A long PPPoE session crosses 4 GiB routinely,
    and ignoring gigawords silently stops counting at the wrap.
  - Traffic inside a daily policy's free window is recorded (usage_daily raw
    columns) but neither counted toward FUP nor decremented from quotas.

ClickHouse history is written by the caller after the response (it's
best-effort telemetry and its client is synchronous — it must never sit on
the accounting hot path).
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.plan import DailyPolicy, Plan, Subscription
from app.models.quota import QuotaState
from app.models.session import RadiusSession
from app.models.subscriber import Subscriber
from app.models.usage import UsageDaily
from app.policy.authorize_service import daily_policy_spec
from app.policy.clock import policy_day, to_local
from app.policy.quota_engine import (
    AccountingDelta,
    QuotaKind,
    QuotaLedger,
    QuotaPeriod,
    apply_usage,
    quota_field_name,
)

_ALL_KEYS = [(k, p) for k in QuotaKind for p in QuotaPeriod]
_WRAP = 1 << 32


@dataclass(frozen=True)
class AccountingResult:
    subscriber_id: uuid.UUID
    delta: AccountingDelta
    free_time: bool


def total_octets(octets: int, gigawords: int) -> int:
    return (int(gigawords or 0) << 32) + int(octets or 0)


def counter_delta(current: int, last: int) -> int:
    """Increment between two cumulative readings. A large backwards jump is a
    32-bit wrap on a NAS that doesn't send gigawords; a small one is a
    duplicate/out-of-order packet and counts as zero."""
    if current >= last:
        return current - last
    if last - current > _WRAP // 2:
        return current + _WRAP - last
    return 0


async def _is_free_time(db: AsyncSession, subscriber_id, now: datetime) -> bool:
    plan_policy = (
        await db.execute(
            select(DailyPolicy)
            .join(Plan, Plan.daily_policy_id == DailyPolicy.id)
            .join(Subscription, Subscription.plan_id == Plan.id)
            .where(Subscription.subscriber_id == subscriber_id)
        )
    ).scalar_one_or_none()
    if plan_policy is None or not plan_policy.free_windows:
        return False
    return daily_policy_spec(plan_policy).is_free_time(to_local(now))


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
    acct_input_gigawords: int = 0,
    acct_output_gigawords: int = 0,
    framed_ip: str | None = None,
    calling_station_id: str | None = None,
    nas_port_id: str | None = None,
    initial_reply: dict | None = None,
) -> AccountingResult | None:
    subscriber = (
        await db.execute(select(Subscriber).where(Subscriber.username == username))
    ).scalar_one_or_none()
    if subscriber is None:
        return None  # unknown username in an accounting packet: nothing to decrement against

    session_row = (
        await db.execute(select(RadiusSession).where(RadiusSession.acct_session_id == acct_session_id))
    ).scalar_one_or_none()

    if session_row is None:
        # Counters passed explicitly rather than left to their column
        # default=0: a column default only applies at INSERT, but the delta
        # computation below reads them on the in-memory object first.
        session_row = RadiusSession(
            acct_session_id=acct_session_id,
            subscriber_id=subscriber.id,
            nas_ip=nas_ip,
            username=username,
            last_input_octets=0,
            last_output_octets=0,
            last_session_time_s=0,
            last_reply=initial_reply,
            updated_at=now,
        )
        db.add(session_row)

    in_total = total_octets(acct_input_octets, acct_input_gigawords)
    out_total = total_octets(acct_output_octets, acct_output_gigawords)
    delta = AccountingDelta(
        rx_bytes=counter_delta(in_total, session_row.last_input_octets),
        tx_bytes=counter_delta(out_total, session_row.last_output_octets),
        session_seconds=max(0, (acct_session_time or 0) - session_row.last_session_time_s),
    )

    session_row.last_input_octets = in_total
    session_row.last_output_octets = out_total
    session_row.last_session_time_s = acct_session_time or 0
    session_row.updated_at = now
    session_row.nas_ip = nas_ip or session_row.nas_ip
    session_row.username = username
    if framed_ip:
        session_row.framed_ip = framed_ip
    if calling_station_id:
        session_row.calling_station_id = calling_station_id
    if nas_port_id:
        session_row.nas_port_id = nas_port_id
    if acct_status_type == "Stop":
        session_row.stopped_at = now
        session_row.terminate_cause = acct_terminate_cause or None
    elif session_row.stopped_at is not None and acct_status_type in ("Start", "Interim-Update"):
        # Interim after the stale sweep closed it: the session was alive after all.
        session_row.stopped_at = None
        session_row.terminate_cause = None
    subscriber.last_online = now

    free = await _is_free_time(db, subscriber.id, now)
    counted_rx = 0 if free else delta.rx_bytes
    counted_tx = 0 if free else delta.tx_bytes

    if delta.rx_bytes or delta.tx_bytes or delta.session_seconds:
        stmt = pg_insert(UsageDaily).values(
            subscriber_id=subscriber.id,
            day=policy_day(now),
            rx_bytes=delta.rx_bytes,
            tx_bytes=delta.tx_bytes,
            uptime_s=delta.session_seconds,
            counted_rx_bytes=counted_rx,
            counted_tx_bytes=counted_tx,
            updated_at=now,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[UsageDaily.subscriber_id, UsageDaily.day],
            set_={
                "rx_bytes": UsageDaily.rx_bytes + stmt.excluded.rx_bytes,
                "tx_bytes": UsageDaily.tx_bytes + stmt.excluded.tx_bytes,
                "uptime_s": UsageDaily.uptime_s + stmt.excluded.uptime_s,
                "counted_rx_bytes": UsageDaily.counted_rx_bytes + stmt.excluded.counted_rx_bytes,
                "counted_tx_bytes": UsageDaily.counted_tx_bytes + stmt.excluded.counted_tx_bytes,
                "updated_at": stmt.excluded.updated_at,
            },
        )
        await db.execute(stmt)

    if not free:
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

    return AccountingResult(subscriber_id=subscriber.id, delta=delta, free_time=free)
