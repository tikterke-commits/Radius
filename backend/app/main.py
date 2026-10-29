import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.admin import router as admin_router
from app.api.radius import router as radius_router
from app.db import SessionLocal
from app.policy.authorize_service import warm_up

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        async with SessionLocal() as db:
            await warm_up(db)
    except Exception:
        logger.exception("authorize warm-up failed; first logins may hit the cold path")
    yield


app = FastAPI(title="Data Plus Lebanon ISP Platform — RADIUS engine", lifespan=lifespan)

app.include_router(radius_router)
app.include_router(admin_router)


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}
