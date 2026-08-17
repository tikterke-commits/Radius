import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, Numeric, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.common import created_at, tenant_fk, uuid_pk


class Balance(Base):
    """balance and loan_balance are separate concepts per A6: prepaid balance
    the subscriber has paid in, vs credit/loan extended to them. Never merge
    these into one number.
    """

    __tablename__ = "balances"

    subscriber_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id"), primary_key=True
    )
    balance: Mapped[Numeric] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    loan_balance: Mapped[Numeric] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class InvoiceStatus(str, enum.Enum):
    DRAFT = "draft"
    ISSUED = "issued"
    PAID = "paid"
    OVERDUE = "overdue"
    VOID = "void"


class Invoice(Base):
    __tablename__ = "invoices"

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    subscriber_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id"), nullable=False, index=True
    )
    amount: Mapped[Numeric] = mapped_column(Numeric(12, 2), nullable=False)
    status: Mapped[InvoiceStatus] = mapped_column(
        Enum(InvoiceStatus, name="invoice_status"), nullable=False, default=InvoiceStatus.DRAFT
    )
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at()


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[uuid.UUID] = uuid_pk()
    invoice_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("invoices.id"), nullable=False)
    amount: Mapped[Numeric] = mapped_column(Numeric(12, 2), nullable=False)
    method: Mapped[str] = mapped_column(String(40), nullable=False)  # cash, card, voucher, ...
    created_at: Mapped[datetime] = created_at()


class Voucher(Base):
    """Prepaid top-up card with attempt limiting (A6) — a fixed number of
    redemption attempts before the code is locked, to blunt brute-forcing
    card codes.
    """

    __tablename__ = "vouchers"

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    code_hash: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    value: Mapped[Numeric] = mapped_column(Numeric(12, 2), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attempt_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    locked: Mapped[bool] = mapped_column(default=False)
    redeemed_by_subscriber_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    redeemed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at()


class Addon(Base):
    """A one-off add-on package (extra quota, speed boost, etc.)."""

    __tablename__ = "addons"

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    price: Mapped[Numeric] = mapped_column(Numeric(12, 2), nullable=False)
    grants: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict
    )  # e.g. {"extra_rxtx_bytes": ..., "speed_boost_bps": ...}


class AddonSubscription(Base):
    __tablename__ = "addon_subscriptions"

    id: Mapped[uuid.UUID] = uuid_pk()
    subscriber_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id"), nullable=False, index=True
    )
    addon_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("addons.id"), nullable=False)
    purchased_at: Mapped[datetime] = created_at()
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
