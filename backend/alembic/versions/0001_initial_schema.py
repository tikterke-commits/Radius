"""Initial schema: tenants, subscribers, credentials, plans, quotas, billing,
RBAC, audit log, NAS inventory, FreeRADIUS SQL-fallback tables.

Written by creating all tables from the current SQLAlchemy metadata rather
than hand-written op.create_table() calls, because this session has no live
Postgres to run `alembic revision --autogenerate` against (no docker daemon
available in this sandbox — see docs/00-part-h-design.md §0). This is the one
migration where that's acceptable: it's the very first one, so "current
models" and "desired initial state" are the same thing by definition. Every
migration after this one should be generated with --autogenerate against a
real database, reviewed, and committed as an explicit op.* diff — not written
this way again.

Revision ID: 0001
Revises:
Create Date: 2026-08-17
"""

from typing import Sequence, Union

from alembic import op

from app.db import Base
from app import models  # noqa: F401

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind)
