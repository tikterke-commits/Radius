import uuid
from datetime import datetime

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, ForeignKey, Integer, Numeric, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.common import created_at, tenant_fk, uuid_pk

# Enum-like string columns are validated at the API layer (Literal types in
# app/schemas/admin.py) rather than as Postgres ENUM types — adding an action
# later is then a code change, not an ALTER TYPE migration.
EXHAUST_ACTIONS = ("block", "throttle", "walled_garden")
EXPIRED_ACTIONS = ("block", "walled_garden")
MONTHLY_RESET_MODES = ("renewal", "calendar")
COUNTED_TRAFFIC = ("rxtx", "tx", "rx")


class TimeOfDayPolicy(Base):
    """First-class scheduled policy primitive (A4), not a static router rule.

    windows: list of
      {"start": "17:00", "end": "02:00", "days": ["mon".."sun"],
       "applies_to": "all" | "international" | <traffic class>,
       "speed_multiplier": 0.25,              # optional
       "rx_limit_bps": .., "tx_limit_bps": .., # optional absolute override
       "address_list": "international-peak"}  # optional, class windows only

    applies_to="all" changes the subscriber's own Mikrotik-Rate-Limit (the
    "simple queue drops to 25% of plan at peak" policy, pushed by CoA at the
    window edges). Any other class leaves the rate alone and tags the
    subscriber into an address list the router's queue tree keys off.
    """

    __tablename__ = "time_of_day_policies"

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    windows: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    created_at: Mapped[datetime] = created_at()


class DailyPolicy(Base):
    """SAS4-style "Daily Policy": a reusable daily traffic regime attached to
    a profile. Counts usage per policy day (rolls over at
    settings.daily_reset_time, local), excluding free windows.

    tiers: [{"after_bytes": 5e9, "rx_limit_bps": .., "tx_limit_bps": ..}]
        daily FUP — highest crossed threshold wins.
    daily_quota_bytes + on_exhaust: what happens once today's counted usage
        hits the cap (block / throttle to exhausted_* / walled garden).
    free_windows: [{"start": "02:00", "end": "08:00", "days": [...]}]
        traffic in these windows is recorded but not counted against the
        daily policy or the profile's hard quotas.
    """

    __tablename__ = "daily_policies"

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[str | None] = mapped_column(String(255))

    counted: Mapped[str] = mapped_column(String(8), nullable=False, default="rxtx")
    tiers: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    daily_quota_bytes: Mapped[int | None] = mapped_column(BigInteger)
    on_exhaust: Mapped[str] = mapped_column(String(16), nullable=False, default="throttle")
    exhausted_rx_bps: Mapped[int | None] = mapped_column(BigInteger)
    exhausted_tx_bps: Mapped[int | None] = mapped_column(BigInteger)

    free_windows: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    created_at: Mapped[datetime] = created_at()


class Plan(Base):
    """A service profile ("Profile" in the Atlas UI, "plan" in code).

    profile_name_normalized is the collapsed value (~8 real tiers) that the
    import's profile_map.yaml maps ~47 legacy names onto (Part F).

    Rate fields follow the platform-wide direction convention: rx = what the
    router receives from the subscriber (their upload), tx = what it sends
    them (their download) — the same order Mikrotik-Rate-Limit uses.
    """

    __tablename__ = "plans"

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    profile_name_normalized: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(String(255))
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")

    # Commercial
    price: Mapped[Numeric] = mapped_column(Numeric(12, 2), nullable=False, default=0, server_default="0")
    validity_days: Mapped[int] = mapped_column(Integer, nullable=False, default=30, server_default="30")

    # QoS — Mikrotik-Rate-Limit fields
    rx_limit_bps: Mapped[int] = mapped_column(BigInteger, nullable=False)  # subscriber upload ceiling
    tx_limit_bps: Mapped[int] = mapped_column(BigInteger, nullable=False)  # subscriber download ceiling
    burst_rx_bps: Mapped[int | None] = mapped_column(BigInteger)
    burst_tx_bps: Mapped[int | None] = mapped_column(BigInteger)
    burst_threshold_rx_bps: Mapped[int | None] = mapped_column(BigInteger)
    burst_threshold_tx_bps: Mapped[int | None] = mapped_column(BigInteger)
    burst_time_s: Mapped[int | None] = mapped_column(Integer)
    priority: Mapped[int | None] = mapped_column(Integer)  # 1 (highest) .. 8
    min_rx_bps: Mapped[int | None] = mapped_column(BigInteger)  # limit-at
    min_tx_bps: Mapped[int | None] = mapped_column(BigInteger)

    # NAS-side session attributes
    ip_pool: Mapped[str | None] = mapped_column(String(64))  # Framed-Pool
    mikrotik_group: Mapped[str | None] = mapped_column(String(64))  # PPP profile on the router
    address_list: Mapped[str | None] = mapped_column(String(64))  # Mikrotik-Address-List
    session_timeout_s: Mapped[int | None] = mapped_column(Integer)
    idle_timeout_s: Mapped[int | None] = mapped_column(Integer)
    interim_interval_s: Mapped[int | None] = mapped_column(Integer)  # Acct-Interim-Interval
    # Emit Mikrotik-Total-Limit(+Gigawords) = tightest remaining hard quota,
    # so the router cuts the session at exhaustion without waiting for CoA.
    enforce_quota_on_nas: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # [{"attribute": "Mikrotik-Advertise-URL", "value": "..."}] — appended to
    # the Access-Accept as-is. Attribute must exist in the FreeRADIUS dictionary.
    custom_attributes: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")

    # Hard quotas (gate-enforced)
    quota_rx_monthly: Mapped[int | None] = mapped_column(BigInteger)
    quota_tx_monthly: Mapped[int | None] = mapped_column(BigInteger)
    quota_rxtx_monthly: Mapped[int | None] = mapped_column(BigInteger)
    quota_uptime_monthly_s: Mapped[int | None] = mapped_column(BigInteger)
    quota_rx_daily: Mapped[int | None] = mapped_column(BigInteger)
    quota_tx_daily: Mapped[int | None] = mapped_column(BigInteger)
    quota_rxtx_daily: Mapped[int | None] = mapped_column(BigInteger)
    quota_uptime_daily_s: Mapped[int | None] = mapped_column(BigInteger)
    monthly_reset: Mapped[str] = mapped_column(
        String(16), nullable=False, default="renewal", server_default="renewal"
    )

    on_quota_exhausted: Mapped[str] = mapped_column(
        String(16), nullable=False, default="block", server_default="block"
    )
    exhausted_rx_bps: Mapped[int | None] = mapped_column(BigInteger)
    exhausted_tx_bps: Mapped[int | None] = mapped_column(BigInteger)
    on_expired: Mapped[str] = mapped_column(String(16), nullable=False, default="block", server_default="block")

    fup_tiers: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list
    )  # monthly FUP: [{"after_bytes": ..., "rx_limit_bps": ..., "tx_limit_bps": ...}, ...]

    daily_policy_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("daily_policies.id")
    )
    time_of_day_policy_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("time_of_day_policies.id")
    )
    default_concurrent_sessions: Mapped[int] = mapped_column(default=1)

    created_at: Mapped[datetime] = created_at()


class Subscription(Base):
    """Which plan a subscriber is on, plus the scheduled-change field the legacy
    system already trained users to expect (next_profile_change).

    period_started_at is the start of the current monthly quota period — the
    renewal moment for monthly_reset="renewal" plans, the 1st of the month
    (local) for "calendar" plans. Monthly FUP consumption is summed from here.
    """

    __tablename__ = "subscriptions"

    subscriber_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id"), primary_key=True
    )
    plan_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("plans.id"), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expiration: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    period_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

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
