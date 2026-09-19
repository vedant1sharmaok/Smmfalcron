"""Health check endpoints."""
from __future__ import annotations
from fastapi import APIRouter
from app.core.database import check_db_health
from app.core.redis import check_redis_health

router = APIRouter()


@router.get("/health")
async def health():
    return {"status": "ok"}


@router.get("/ready")
async def ready():
    db_ok    = await check_db_health()
    redis_ok = await check_redis_health()
    if db_ok and redis_ok:
        return {"status": "ready", "db": "ok", "redis": "ok"}
    return {"status": "not_ready", "db": "ok" if db_ok else "error",
            "redis": "ok" if redis_ok else "error"}
