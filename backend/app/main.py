from fastapi import FastAPI

from app.api.radius import router as radius_router

app = FastAPI(title="Data Plus Lebanon ISP Platform")

app.include_router(radius_router)


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}
