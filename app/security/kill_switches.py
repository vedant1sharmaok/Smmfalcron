"""
Named kill switches backed by Redis.

10 switches — any can be activated to immediately block the named operation:
  all_orders         — blocks all order creation (any source)
  bot_orders         — blocks bot-originating orders
  api_orders         — blocks own-SMM-API orders
  miniapp_orders     — blocks Mini App orders
  all_deposits       — blocks all payment intents
  razorpay_deposits  — blocks Razorpay specifically
  stripe_deposits    — blocks Stripe specifically
  provider_sync      — blocks provider sync runs
  all_refills        — blocks refill requests
  all_cancels        — blocks cancel requests

Each switch stores a JSON payload with: reason, activated_by, activated_at, ttl_s
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from app.core.exceptions import KillSwitchError
from app.core.logging import get_logger
from app.core.redis import get_redis

logger = get_logger(__name__)

VALID_SWITCHES = frozenset({
    "all_orders", "bot_orders", "api_orders", "miniapp_orders",
    "all_deposits", "razorpay_deposits", "stripe_deposits",
    "provider_sync", "all_refills", "all_cancels",
})

_KS_KEY = "ks:{name}"


def _key(name: str) -> str:
    return f"ks:{name}"


async def activate(
    name: str,
    reason: str,
    actor_id: int,
    ttl_s: int | None = None,
) -> None:
    """Activate a kill switch. Raises ValueError for unknown switch names."""
    if name not in VALID_SWITCHES:
        raise ValueError(f"Unknown kill switch: {name!r}. Valid: {sorted(VALID_SWITCHES)}")

    payload = json.dumps({
        "reason":       reason,
        "activated_by": actor_id,
        "activated_at": datetime.now(timezone.utc).isoformat(),
        "ttl_s":        ttl_s,
    })
    r = get_redis()
    if ttl_s:
        await r.set(_key(name), payload, ex=ttl_s)
    else:
        await r.set(_key(name), payload)

    logger.warning("kill_switch_activated", name=name, reason=reason, actor_id=actor_id)


async def deactivate(name: str, actor_id: int) -> None:
    """Deactivate a kill switch."""
    if name not in VALID_SWITCHES:
        raise ValueError(f"Unknown kill switch: {name!r}")
    r = get_redis()
    await r.delete(_key(name))
    logger.info("kill_switch_deactivated", name=name, actor_id=actor_id)


async def is_active(name: str) -> bool:
    """Return True if a kill switch is currently active."""
    r = get_redis()
    return bool(await r.exists(_key(name)))


async def get_all_active() -> dict[str, dict]:
    """Return all currently active kill switches with their payloads."""
    r       = get_redis()
    active  = {}
    for name in VALID_SWITCHES:
        raw = await r.get(_key(name))
        if raw:
            try:
                active[name] = json.loads(raw)
            except Exception:
                active[name] = {"raw": raw}
    return active


async def assert_orders_allowed(source: str = "all") -> None:
    """
    Assert that orders can be placed from the given source.
    Raises KillSwitchError if any relevant switch is active.
    Called at the start of the order pipeline — before any DB writes.
    """
    checks = ["all_orders"]
    if source == "bot":      checks.append("bot_orders")
    elif source == "api":    checks.append("api_orders")
    elif source == "miniapp":checks.append("miniapp_orders")

    r = get_redis()
    for name in checks:
        raw = await r.get(_key(name))
        if raw:
            payload = json.loads(raw) if raw else {}
            raise KillSwitchError(
                detail=f"Kill switch '{name}' is active: {payload.get('reason','')}",
                switch_name=name,
            )


async def assert_deposits_allowed(provider: str = "all") -> None:
    """Assert deposits are allowed for the given provider."""
    checks = ["all_deposits"]
    if provider == "razorpay": checks.append("razorpay_deposits")
    elif provider == "stripe": checks.append("stripe_deposits")

    r = get_redis()
    for name in checks:
        raw = await r.get(_key(name))
        if raw:
            payload = json.loads(raw) if raw else {}
            raise KillSwitchError(
                detail=f"Kill switch '{name}' is active",
                switch_name=name,
            )
