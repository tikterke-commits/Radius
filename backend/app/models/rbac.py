import enum
import uuid
from datetime import datetime

from sqlalchemy import Enum, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.common import created_at, tenant_fk, uuid_pk


class RoleName(str, enum.Enum):
    TENANT_ADMIN = "tenant_admin"
    NOC_ENGINEER = "noc_engineer"
    TECH_SUPPORT = "tech_support"  # A7: subscriber-scoped, no uplink/CDN aggregate view
    BILLING_CLERK = "billing_clerk"
    PORTAL_SUBSCRIBER = "portal_subscriber"


class Manager(Base):
    """An admin/staff account (distinct from Subscriber, which is a customer)."""

    __tablename__ = "managers"

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    email: Mapped[str] = mapped_column(unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = created_at()


class RoleAssignment(Base):
    """Which role a manager holds, scoped to a tenant. Row-level scoping for
    tech_support (A7: "row-scoped to subscribers under a given parent tenant")
    is enforced by Postgres RLS policies keyed on tenant_id, not by this table
    alone — this table records the grant, RLS enforces the boundary.
    """

    __tablename__ = "role_assignments"

    id: Mapped[uuid.UUID] = uuid_pk()
    manager_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("managers.id"), nullable=False)
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    role: Mapped[RoleName] = mapped_column(Enum(RoleName, name="role_name"), nullable=False)
