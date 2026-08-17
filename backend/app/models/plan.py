import uuid
from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.common import created_at, tenant_fk, uuid_pk


class TimeOfDayPolicy(Base):
    """First-class scheduled policy primitive (A4), not a static router rule.

    windows: list of {start, end, days, speed_multiplier, applies_to} e.g.
      {"start": "17:00", "end": "24:00", "days": ["mon".."sun"],
       "speed_multiplier": 0.25, "applies_to": "international"}
    Expressible per plan (via Plan.time_of_day_policy_id) and per subscriber
    via SubscriberPolicyException below.
    """

    __tablename__ = "time_of_day_policies"

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    windows: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    created_at: Mapped[datetime] = created_at()


class Plan(Base):
    """A billing/service tier. profile_name_normalized is the collapsed value
    (~8 real tiers) that the import's profile_map.yaml maps ~47 legacy names
    onto (Part F).
    """

    __tablename__ = "plans"

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    profile_name_normalized: Mapped[str] = mapped_column(String(80), nullable=False, index=True)

    rx_limit_bps: Mapped[int] = mapped_column(BigInteger, nullable=False)  # subscriber upload ceiling
    tx_limit_bps: Mapped[int] = mapped_column(BigInteger, nullable=False)  # subscriber download ceiling

    quota_rx_monthly: Mapped[int | None] = mapped_column(BigInteger)
    quota_tx_monthly: Mapped[int | None] = mapped_column(BigInteger)
    quota_rxtx_monthly: Mapped[int | None] = mapped_column(BigInteger)
    quota_uptime_monthly_s: Mapped[int | None] = mapped_column(BigInteger)
    quota_rx_daily: Mapped[int | None] = mapped_column(BigInteger)
    quota_tx_daily: Mapped[int | None] = mapped_column(BigInteger)
    quota_rxtx_daily: Mapped[int | None] = mapped_column(BigInteger)
    quota_uptime_daily_s: Mapped[int | None] = mapped_column(BigInteger)

    fup_tiers: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list
    )  # [{"after_bytes": ..., "rx_limit_bps": ..., "tx_limit_bps": ...}, ...] tiered degradation

    time_of_day_policy_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("time_of_day_policies.id")
    )
    default_concurrent_sessions: Mapped[int] = mapped_column(default=1)

    created_at: Mapped[datetime] = created_at()


class Subscription(Base):
    """Which plan a subscriber is on, plus the scheduled-change field the legacy
    system already trained users to expect (next_profile_change)."""

    __tablename__ = "subscriptions"

    subscriber_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id"), primary_key=True
    )
    plan_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("plans.id"), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expiration: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    next_profile_change_plan_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("plans.id")
    )
    next_profile_change_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SubscriberPolicyException(Base):
    """Per-subscriber override of a time-of-day policy window (A4: "per plan and
    per subscriber with exceptions")."""

    __tablename__ = "subscriber_policy_exceptions"

    id: Mapped[uuid.UUID] = uuid_pk()
    subscriber_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id"), nullable=False, index=True
    )
    time_of_day_policy_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("time_of_day_policies.id"), nullable=False
    )
    override: Mapped[dict] = mapped_column(JSON, nullable=False)  # partial window override
    reason: Mapped[str | None] = mapped_column(String(255))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
