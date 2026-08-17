"""Import every model module so Base.metadata sees all tables (used by Alembic autogenerate)."""

from app.models import (  # noqa: F401
    audit,
    billing,
    credential,
    nas,
    plan,
    quota,
    radius_fallback,
    rbac,
    session,
    subscriber,
    tenant,
)
