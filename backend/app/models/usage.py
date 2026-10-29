import uuid
from datetime import date, datetime

from sqlalchemy import BigInteger, Date, DateTime, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.common import uuid_pk


class UsageDaily(Base):
    """Per-subscriber usage per *policy day* (local date, rolling over at
    settings.daily_reset_time). ~650 rows/day — small enough for Postgres,
    and it's what the daily policy, monthly FUP and the usage charts read.

    raw = everything the NAS reported. counted = raw minus traffic that
    landed inside a daily policy's free window; FUP and quotas use counted.
    ClickHouse radacct_events keeps the per-packet history underneath.
    """

    __tablename__ = "usage_daily"

    subscriber_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id", ondelete="CASCADE"), primary_key=True
    )
    day: Mapped[date] = mapped_column(Date, primary_key=True)

    rx_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    tx_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    uptime_s: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    counted_rx_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    counted_tx_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AuthEvent(Base):
    """One row per Access-Accept / Access-Reject, with the engine's reason —
    the "why can't this customer connect" screen. Low volume (reconnects,
    not interim updates); pruned after settings.auth_log_retention_days.
    """

    __tablename__ = "auth_events"
    __table_args__ = (Index("ix_auth_events_username_ts", "username", "ts"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False)
    nas_ip: Mapped[str | None] = mapped_column(String(45))
    calling_station_id: Mapped[str | None] = mapped_column(String(64))
    result: Mapped[str] = mapped_column(String(8), nullable=False)  # accept | reject
    reason: Mapped[str | None] = mapped_column(String(255))
