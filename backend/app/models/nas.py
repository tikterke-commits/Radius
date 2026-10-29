import uuid
from datetime import datetime

from sqlalchemy import Boolean, Integer, String
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.common import created_at, tenant_fk, uuid_pk


class NasDevice(Base):
    """A MikroTik BNG/PPPoE server that authenticates against this engine.

    This list is the engine's whole NAS universe — the FreeRADIUS client
    definitions are rendered from it (app/services/nas_clients.py), so a
    PPPoE server that isn't here can't authenticate. The engine is fully
    separate from SAS4: only PPPoE servers dedicated to engine subscribers
    belong here.

    capability_matrix records what this specific box actually does (Part E:
    "branch on it explicitly rather than assuming uniform behaviour across
    ROS6 and ROS7") — e.g.:
      {"coa_rate_change_applies_midsession": true}
    Missing keys default to the permissive answer; set false after a test
    shows a box ignoring a mid-session rate change, and actuation falls back
    to Disconnect + reconnect for it.
    """

    __tablename__ = "nas_devices"

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    ip: Mapped[str] = mapped_column(INET, nullable=False, unique=True)
    secret_encrypted: Mapped[str] = mapped_column(String(255), nullable=False)  # RADIUS shared secret, Fernet
    ros_version: Mapped[str] = mapped_column(String(20), nullable=False)  # "6.49.19", "7.x"
    coa_port: Mapped[int] = mapped_column(Integer, nullable=False, default=3799, server_default="3799")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    description: Mapped[str | None] = mapped_column(String(255))
    capability_matrix: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = created_at()
