import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.common import created_at, uuid_pk


class Tenant(Base):
    """A reseller ISP or the top-level Data Plus Lebanon tenant itself.

    parent_tenant_id replaces the legacy SAS4 parent_id reseller pointer with a
    real hierarchy. Row-level isolation (not schema-per-tenant, see
    docs/00-part-h-design.md §1) is enforced via tenant_id on every scoped table
    plus Postgres RLS policies, not by this table alone.
    """

    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    parent_tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=True
    )
    created_at: Mapped[datetime] = created_at()

    children: Mapped[list["Tenant"]] = relationship(
        "Tenant", backref="parent", remote_side=[id]
    )
