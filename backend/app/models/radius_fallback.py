"""Standard FreeRADIUS SQL schema (radcheck/radreply/radgroupcheck/radgroupreply),
kept as the degraded-fallback path per A1. These are populated by a sync job
from the platform's real subscriber/plan/quota state — the platform is still
the source of truth; this is what FreeRADIUS's rlm_sql module reads directly
if rlm_rest can't reach the backend within budget and the NAS falls through
to SQL-fallback mode in the virtual server's authorize section.

The FreeRADIUS-standard `nas` table is not duplicated here — it's generated
from app.models.nas.NasDevice, which is the platform's real NAS inventory and
carries the capability matrix these fallback tables don't need.

radacct is intentionally NOT modeled here: at production write volume it
belongs in ClickHouse (see docs/00-part-h-design.md §2), not Postgres. If
SQL-fallback accounting is ever needed (rlm_rest down AND ClickHouse down),
that's a compounding-failure scenario handled by local FreeRADIUS detail-file
logging, not a second accounting table to keep in sync.
"""

import uuid

from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class RadCheck(Base):
    __tablename__ = "radcheck"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    username: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    attribute: Mapped[str] = mapped_column(String(64), nullable=False)
    op: Mapped[str] = mapped_column(String(2), nullable=False, default=":=")
    value: Mapped[str] = mapped_column(String(253), nullable=False)


class RadReply(Base):
    __tablename__ = "radreply"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    username: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    attribute: Mapped[str] = mapped_column(String(64), nullable=False)
    op: Mapped[str] = mapped_column(String(2), nullable=False, default="=")
    value: Mapped[str] = mapped_column(String(253), nullable=False)


class RadGroupCheck(Base):
    __tablename__ = "radgroupcheck"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    groupname: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    attribute: Mapped[str] = mapped_column(String(64), nullable=False)
    op: Mapped[str] = mapped_column(String(2), nullable=False, default=":=")
    value: Mapped[str] = mapped_column(String(253), nullable=False)


class RadGroupReply(Base):
    __tablename__ = "radgroupreply"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    groupname: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    attribute: Mapped[str] = mapped_column(String(64), nullable=False)
    op: Mapped[str] = mapped_column(String(2), nullable=False, default="=")
    value: Mapped[str] = mapped_column(String(253), nullable=False)


class RadUserGroup(Base):
    __tablename__ = "radusergroup"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    username: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    groupname: Mapped[str] = mapped_column(String(64), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
