import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.common import created_at


class Credential(Base):
    """PAP-only credential storage. Argon2id hash, never a reversible cipher.

    No NT-hash column by design — see docs/00-part-h-design.md §5. MS-CHAPv2/CHAP
    are not supported by this platform; the FreeRADIUS config only advertises
    PAP for PPPoE auth.

    forced_rotation / rotation_deadline back the staged rotation plan required
    by A5 for the imported credentials, which must all be treated as
    compromised. Nothing in this table is ever returned by an API response.
    """

    __tablename__ = "credentials"

    subscriber_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id"), primary_key=True
    )
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    algo_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    forced_rotation: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    rotation_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    imported_from_sas4: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    created_at: Mapped[datetime] = created_at()
