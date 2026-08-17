import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import INET, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.common import created_at, tenant_fk, uuid_pk


class Subscriber(Base):
    """Core subscriber record. Fields carried forward from the legacy SAS4 detail
    endpoint (username, contact/KYC fields, national_id, contract_id) plus
    platform-native additions (mac_binding, concurrent_session_limit) that the
    legacy system lacked and the live network state showed were needed.

    legacy_sas4_id is import provenance only — never read at runtime after
    cutover (Part F: the import module is flag-gated and quarantined).
    """

    __tablename__ = "subscribers"

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()

    username: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    firstname: Mapped[str | None] = mapped_column(String(80))
    lastname: Mapped[str | None] = mapped_column(String(80))
    company: Mapped[str | None] = mapped_column(String(120))
    phone: Mapped[str | None] = mapped_column(String(30), index=True)  # used for SMS OTP portal login
    email: Mapped[str | None] = mapped_column(String(160))
    address: Mapped[str | None] = mapped_column(String(255))
    city: Mapped[str | None] = mapped_column(String(80))
    notes: Mapped[str | None] = mapped_column(String)

    national_id: Mapped[str | None] = mapped_column(String(60))  # KYC/lawful, carried forward
    contract_id: Mapped[str | None] = mapped_column(String(60))

    static_ip: Mapped[str | None] = mapped_column(INET)
    mac_binding: Mapped[str | None] = mapped_column(String(17))  # absent on ~all legacy accounts
    concurrent_session_limit: Mapped[int | None] = mapped_column(Integer)  # overrides plan default

    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    created_at: Mapped[datetime] = created_at()
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    legacy_sas4_id: Mapped[str | None] = mapped_column(String(64), index=True)

    last_online: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
