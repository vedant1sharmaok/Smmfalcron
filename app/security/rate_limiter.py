"""
Redis sliding window rate limiter.

Keys are scoped by: limiter_type + user_id (or api_key_hash or ip_hash).
This ensures one user hitting their limit cannot affect another user.

Usage:
  from app.security.rate_limiter import check_rate_limit, RateLimitConfig

  # In a FastAPI dependency or middleware:
  await check_rate_limit(user_id=42, action="order", limit=10, window_s=60)
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from app.core.exceptions import RateLimitError
from app.core.logging import get_logger
from app.core.redis import get_redis

logger = get_logger(__name__)


@dataclass
class RateLimitConfig:
    action:   str       # e.g. "order", "deposit", "api_request"
    limit:    int       # max requests per window
    window_s: int       # window in seconds


# Default per-action configs
LIMITS: dict[str, RateLimitConfig] = {
    "order":      RateLimitConfig("order",      10,  60),   # 10 orders/min
    "deposit":    RateLimitConfig("deposit",     5,  60),   # 5 deposits/min
    "api_request":RateLimitConfig("api_request",120, 60),   # 120 req/min
    "bot_message":RateLimitConfig("bot_message", 30,  60),  # 30 msgs/min
    "auth":       RateLimitConfig("auth",        10, 300),  # 10 auth/5min
}


def _rl_key(action: str, user_id: int) -> str:
    """Rate limit key scoped by action + user_id (never shared across users)."""
    window = int(time.time()) // 60   # 1-minute buckets
    return f"rl:{action}:user:{user_id}:{window}"


async def check_rate_limit(
    user_id: int,
    action: str,
    limit: int | None = None,
    window_s: int | None = None,
) -> None:
    """
    Check and increment the rate limit counter for a user + action.
    Raises RateLimitError if the limit is exceeded.
    """
    cfg = LIMITS.get(action, RateLimitConfig(action, limit or 60, window_s or 60))
    max_requests = limit or cfg.limit
    ttl          = window_s or cfg.window_s

    key = _rl_key(action, user_id)
    r   = get_redis()

    pipe    = r.pipeline()
    pipe.incr(key)
    pipe.expire(key, ttl)
    results = await pipe.execute()
    count   = results[0]

    if count > max_requests:
        logger.warning(
            "rate_limit_exceeded",
            user_id=user_id,
            action=action,
            count=count,
            limit=max_requests,
        )
        raise RateLimitError(
            detail=f"Rate limit exceeded: {action} {count}/{max_requests} per {ttl}s",
            retry_after=ttl,
        )


async def check_api_key_rate_limit(
    key_hash: str,
    action: str = "api_request",
    limit: int = 120,
    window_s: int = 60,
) -> None:
    """
    Rate limit by API key hash instead of user_id.
    Used in the Own SMM API routes.
    """
    bucket = int(time.time()) // window_s
    rkey   = f"rl:{action}:key:{key_hash}:{bucket}"
    r      = get_redis()

    pipe    = r.pipeline()
    pipe.incr(rkey)
    pipe.expire(rkey, window_s)
    results = await pipe.execute()
    count   = results[0]

    if count > limit:
        raise RateLimitError(
            detail=f"API key rate limit exceeded: {count}/{limit}",
            retry_after=window_s,
        )


async def get_rate_limit_status(user_id: int, action: str) -> dict:
    """Return current count and limit for monitoring."""
    key = _rl_key(action, user_id)
    cfg = LIMITS.get(action, RateLimitConfig(action, 60, 60))
    r   = get_redis()
    raw = await r.get(key)
    count = int(raw) if raw else 0
    return {
        "action":   action,
        "user_id":  user_id,
        "count":    count,
        "limit":    cfg.limit,
        "window_s": cfg.window_s,
    }
