"""Async Redis connection pool and key namespace."""
from __future__ import annotations
import redis.asyncio as aioredis
from typing import Optional

_redis_pool: Optional[aioredis.Redis] = None


class RedisKeys:
    """Centralised key namespace — prevents typos and collisions."""
    # Sessions
    SESSION          = "session:{session_token}"
    USER_SESSIONS    = "user_sessions:{user_id}"
    # Rate limiting
    RATE_LIMIT       = "rl:{limiter_type}:{key}:{window}"
    # Kill switches
    KILL_SWITCH      = "ks:{switch_name}"
    # FX rates
    FX_USD_INR       = "fx:usd_inr"
    # Order monitor
    ORDER_MONITOR_TS = "om:last_run"
    # Broadcast
    BROADCAST_LOCK   = "bc:lock:{broadcast_id}"

    @staticmethod
    def fmt(template: str, **kwargs) -> str:
        return template.format(**kwargs)

    @staticmethod
    def circuit_breaker(provider_id: int) -> str:
        return f"cb:provider:{provider_id}"


async def init_redis() -> None:
    global _redis_pool
    from app.core.config import settings
    _redis_pool = aioredis.from_url(
        settings.redis_url,
        encoding="utf-8",
        decode_responses=False,
        max_connections=20,
    )


async def close_redis() -> None:
    global _redis_pool
    if _redis_pool:
        await _redis_pool.aclose()
        _redis_pool = None


def get_redis() -> aioredis.Redis:
    if _redis_pool is None:
        raise RuntimeError("Redis not initialised — call init_redis() on startup")
    return _redis_pool


async def check_redis_health() -> bool:
    try:
        r = get_redis()
        await r.ping()
        return True
    except Exception:
        return False
