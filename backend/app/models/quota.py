import enum
import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Enum, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column


from app.db import Base


class QuotaKind(str, enum.Enum):
    RX = "rx"
    TX = "tx"
    RXTX = "rxtx"
    UPTIME = "uptime"


class QuotaPeriod(str, enum.Enum):
    MONTHLY = "monthly"
    DAILY = "daily"


class QuotaState(Base):
    """Durable copy of quota remaining, per A3's four quota types x two periods
    (8 rows per subscriber max). This is the Postgres source of truth on reset
    boundaries; Redis holds the hot-path mirror decremented per accounting
    interim-update (see docs/00-part-h-design.md §2 — same event, two
    destinations: state here, consumption history in ClickHouse).
    """

    __tablename__ = "quota_state"

    subscriber_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id"), primary_key=True
    )
    kind: Mapped[QuotaKind] = mapped_column(Enum(QuotaKind, name="quota_kind"), primary_key=True)
    period: Mapped[QuotaPeriod] = mapped_column(Enum(QuotaPeriod, name="quota_period"), primary_key=True)

    remaining: Mapped[int] = mapped_column(BigInteger, nullable=False)  # bytes, or seconds for UPTIME
    resets_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class GateStatus(Base):
    """The four-gate authorization model (A2). Each gate independently
    queryable and settable, so support can be shown exactly which gate failed
    instead of a single opaque `active` boolean.
    """

    __tablename__ = "gate_status"

    subscriber_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id"), primary_key=True
    )
    status_gate: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)  # account enabled
    traffic_gate: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)  # quota remaining
    expiration_gate: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)  # not expired
    uptime_gate: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)  # uptime quota remaining
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    @property
    def all_pass(self) -> bool:
        return self.status_gate and self.traffic_gate and self.expiration_gate and self.uptime_gate

    def failed_gates(self) -> list[str]:
        gates = {
            "status": self.status_gate,
            "traffic": self.traffic_gate,
            "expiration": self.expiration_gate,
            "uptime": self.uptime_gate,
        }
        return [name for name, passed in gates.items() if not passed]
