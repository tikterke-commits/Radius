import uuid
from datetime import datetime

from sqlalchemy import String
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.common import created_at, tenant_fk, uuid_pk


class NasDevice(Base):
    """A MikroTik BNG/PPPoE server. capability_matrix records what this
    specific box actually does (Part E: "branch on it explicitly rather than
    assuming uniform behaviour across ROS6 and ROS7") — e.g.:
      {"coa_rate_change_applies_midsession": false,  # ROS6 6.49.19: falls back to PoD+reconnect
       "supports_ip_accounting": false,               # ROS7: removed /ip accounting
       "hc_counters": true}
    Populated by the NAS capability probe, not assumed from ros_version alone.
    """

    __tablename__ = "nas_devices"

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    ip: Mapped[str] = mapped_column(INET, nullable=False, unique=True)
    secret_encrypted: Mapped[str] = mapped_column(String(255), nullable=False)  # RADIUS shared secret, encrypted at rest
    ros_version: Mapped[str] = mapped_column(String(20), nullable=False)  # "6.49.19", "7.x"
    capability_matrix: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = created_at()
