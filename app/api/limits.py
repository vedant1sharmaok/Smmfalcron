"""Per-user and per-IP request budgets for the Mini App API."""

from __future__ import annotations

from fastapi import HTTPException, Request

from app.config import get_settings
from app.ratelimit import RateLimiter

_user: RateLimiter | None = None
_ip: RateLimiter | None = None
_order: RateLimiter | None = None


def _limiters() -> tuple[RateLimiter, RateLimiter, RateLimiter]:
    global _user, _ip, _order
    if _user is None or _ip is None or _order is None:
        s = get_settings()
        _user = RateLimiter(s.rate_limit_per_minute, 60.0)
        _ip = RateLimiter(s.ip_rate_limit_per_minute, 60.0)
        _order = RateLimiter(s.order_rate_limit_per_minute, 60.0)
    return _user, _ip, _order


def reset_limiters() -> None:
    global _user, _ip, _order
    _user = _ip = _order = None


def client_ip(request: Request) -> str:
    if get_settings().trust_proxy:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()[:64]
    return (request.client.host if request.client else "unknown")[:64]


def _too_many(limiter: RateLimiter, key: str) -> HTTPException:
    return HTTPException(
        status_code=429,
        detail="Too many requests. Please slow down.",
        headers={"Retry-After": str(limiter.retry_after(key))},
    )


def check_ip(request: Request) -> None:
    _, ip, _ = _limiters()
    key = client_ip(request)
    if not ip.allow(key):
        raise _too_many(ip, key)


def check_user(telegram_id: int) -> None:
    user, _, _ = _limiters()
    key = str(telegram_id)
    if not user.allow(key):
        raise _too_many(user, key)


def check_order(telegram_id: int) -> None:
    _, _, order = _limiters()
    key = str(telegram_id)
    if not order.allow(key):
        raise _too_many(order, key)
