"""Admin API, mounted at /api/v1. Consumed by Atlas's FreeRADIUS section; every
route requires X-API-Key and records X-Actor in the audit log."""

from fastapi import APIRouter

from app.api.admin import ops, policies, subscribers

router = APIRouter(prefix="/api/v1", tags=["admin"])
router.include_router(policies.router)
router.include_router(subscribers.router)
router.include_router(ops.router)
