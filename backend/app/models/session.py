import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.common import created_at


class RadiusSession(Base):
    """Tracks cumulative counters per RADIUS accounting session so interim
    updates (which report session-cumulative octets, not deltas — this is
    how MikroTik and the RADIUS accounting model work) can be turned into
    the incremental AccountingDelta the quota engine actually consumes.

    Also the backing store for the live-sessions screen and for actuation:
    stopped_at IS NULL (and updated recently) is "believed live", and
    last_reply is the attribute set this session was last given — by the
    Access-Accept or by a later CoA — which reconcile diffs against.
    """

    __tablename__ = "radius_sessions"

    acct_session_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    subscriber_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id"), nullable=False, index=True
    )
    nas_ip: Mapped[str] = mapped_column(String(45), nullable=False)
    username: Mapped[str | None] = mapped_column(String(64), index=True)
    framed_ip: Mapped[str | None] = mapped_column(String(45))
    calling_station_id: Mapped[str | None] = mapped_column(String(64))  # CPE MAC
    nas_port_id: Mapped[str | None] = mapped_column(String(64))

    # 64-bit totals (octets + gigawords << 32), not the raw wrapping 32-bit fields.
    last_input_octets: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    last_output_octets: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    last_session_time_s: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    last_reply: Mapped[dict | None] = mapped_column(JSONB)
    last_actuated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    started_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    terminate_cause: Mapped[str | None] = mapped_column(String(40))
