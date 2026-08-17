import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.common import created_at


class RadiusSession(Base):
    """Tracks cumulative counters per RADIUS accounting session so interim
    updates (which report session-cumulative octets, not deltas — this is
    how MikroTik and the RADIUS accounting model work) can be turned into
    the incremental AccountingDelta the quota engine actually consumes.

    Also the backing store for two things Part E asks for directly:
    the live-sessions UI screen (B9), and the reconciliation sweep of
    "live on router vs believed-live in platform" (Part E) — stopped_at
    IS NULL is "believed live" from the platform's side.
    """

    __tablename__ = "radius_sessions"

    acct_session_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    subscriber_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id"), nullable=False, index=True
    )
    nas_ip: Mapped[str] = mapped_column(String(45), nullable=False)

    last_input_octets: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    last_output_octets: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    last_session_time_s: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    started_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
