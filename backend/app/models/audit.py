import uuid
from datetime import datetime

from sqlalchemy import String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.common import created_at, tenant_fk, uuid_pk


class AuditLog(Base):
    """Every policy-affecting action, including NAS-facing ones. Part E requires
    every speed change to be an audited event carrying trigger, old value, new
    value, and NAS response — nas_response captures that last part, which a
    generic actor/action/old/new log usually omits.
    """

    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    actor: Mapped[str] = mapped_column(String(120), nullable=False)  # manager id, "system:coa", etc.
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    subject: Mapped[str] = mapped_column(String(120), nullable=False)  # e.g. "subscriber:<id>"
    old_value: Mapped[dict | None] = mapped_column(JSONB)
    new_value: Mapped[dict | None] = mapped_column(JSONB)
    nas_response: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = created_at()
