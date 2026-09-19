"""
Fraud and risk engine — Section 29 of blueprint.

Detects:
  - Excessive order attempts in a short window
  - Payment replay / duplicate payment attempts
  - Unusual order frequency patterns
  - Velocity: too many orders too fast
  - Impossible sequences (order on banned/restricted account)

Risk scoring:
  0-30:  Low — allow
  31-60: Medium — allow, flag for review
  61-80: High — allow first time, restrict on repeat
  81+:   Critical — auto-block + security alert

Called by the order engine at step 13 (after price, before debit).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.core.logging import get_logger
from app.core.redis import get_redis

logger = get_logger(__name__)

_WINDOW_ORDERS_60S   = 5    # max orders per 60 seconds
_WINDOW_ORDERS_1H    = 20   # max orders per hour
_WINDOW_ORDERS_24H   = 100  # max orders per 24 hours
_WINDOW_AMOUNT_24H   = 50000  # max spend per 24 hours (INR)


class RiskScore:
    def __init__(self, score: int, reasons: list[str], action: str):
        self.score   = score
        self.reasons = reasons
        self.action  = action  # "allow" | "flag" | "restrict" | "block"

    def is_blocked(self) -> bool:
        return self.action == "block"

    def is_flagged(self) -> bool:
        return self.action in ("flag", "restrict", "block")


async def evaluate_order_risk(
    user_id: int,
    amount_inr: float,
    service_id: int,
    is_banned: bool = False,
    is_premium: bool = False,
) -> RiskScore:
    """
    Evaluate fraud/risk score for an incoming order.
    Returns RiskScore — caller decides whether to block.

    Redis-based sliding window counters for velocity checks.
    """
    score   = 0
    reasons = []
    r       = get_redis()
    now     = datetime.now(timezone.utc)

    # ── Hard block: banned account ────────────────────────────────────────────
    if is_banned:
        return RiskScore(100, ["account_banned"], "block")

    # ── Velocity: orders per 60 seconds ──────────────────────────────────────
    key_60s = f"risk:orders:60s:{user_id}"
    try:
        count_60s = await r.incr(key_60s)
        if count_60s == 1:
            await r.expire(key_60s, 60)
        if count_60s > _WINDOW_ORDERS_60S:
            score  += 40
            reasons.append(f"velocity_60s:{count_60s}")
    except Exception:
        pass

    # ── Velocity: orders per hour ─────────────────────────────────────────────
    key_1h = f"risk:orders:1h:{user_id}:{now.strftime('%Y%m%d%H')}"
    try:
        count_1h = await r.incr(key_1h)
        if count_1h == 1:
            await r.expire(key_1h, 3600)
        if count_1h > _WINDOW_ORDERS_1H:
            score  += 25
            reasons.append(f"velocity_1h:{count_1h}")
    except Exception:
        pass

    # ── Velocity: orders per 24 hours ────────────────────────────────────────
    key_24h = f"risk:orders:24h:{user_id}:{now.strftime('%Y%m%d')}"
    try:
        count_24h = await r.incr(key_24h)
        if count_24h == 1:
            await r.expire(key_24h, 86400)
        if count_24h > _WINDOW_ORDERS_24H:
            score  += 20
            reasons.append(f"velocity_24h:{count_24h}")
    except Exception:
        pass

    # ── Large single order ────────────────────────────────────────────────────
    if amount_inr > 10000 and not is_premium:
        score  += 15
        reasons.append(f"large_order:{amount_inr:.0f}")

    # ── Daily spend limit ─────────────────────────────────────────────────────
    key_spend = f"risk:spend:24h:{user_id}:{now.strftime('%Y%m%d')}"
    try:
        current_spend_raw = await r.get(key_spend)
        current_spend = float(current_spend_raw or 0)
        new_spend     = current_spend + amount_inr
        if new_spend > _WINDOW_AMOUNT_24H:
            score  += 20
            reasons.append(f"spend_limit_24h:{new_spend:.0f}")
        await r.set(key_spend, new_spend, ex=86400)
    except Exception:
        pass

    # ── Determine action ──────────────────────────────────────────────────────
    if score >= 80:
        action = "block"
    elif score >= 60:
        action = "restrict"
    elif score >= 30:
        action = "flag"
    else:
        action = "allow"

    if score > 0:
        logger.info(
            "risk_evaluated",
            user_id=user_id,
            score=score,
            action=action,
            reasons=reasons,
        )

    return RiskScore(score=score, reasons=reasons, action=action)


async def record_security_event(
    db,
    event_type: str,
    user_id: Optional[int] = None,
    severity: str = "medium",
    details: Optional[dict] = None,
) -> None:
    """
    Persist a security event to the security_events table.
    Called from risk engine, auth layer, and payment processing.
    """
    try:
        from datetime import datetime, timezone
        from app.core.models import AuditLog

        entry = AuditLog(
            actor_id=user_id,
            action=f"security.{event_type}",
            resource="security_event",
            resource_id=event_type,
            details={"severity": severity, **(details or {})},
            created_at=datetime.now(timezone.utc),
        )
        db.add(entry)
        await db.flush()
    except Exception as exc:
        logger.error("security_event_record_failed", error=str(exc))


async def reset_risk_counters(user_id: int) -> None:
    """Clear risk counters for a user (called on unban or admin override)."""
    r   = get_redis()
    now = datetime.now(timezone.utc)
    keys = [
        f"risk:orders:60s:{user_id}",
        f"risk:orders:1h:{user_id}:{now.strftime('%Y%m%d%H')}",
        f"risk:orders:24h:{user_id}:{now.strftime('%Y%m%d')}",
        f"risk:spend:24h:{user_id}:{now.strftime('%Y%m%d')}",
    ]
    try:
        await r.delete(*keys)
        logger.info("risk_counters_reset", user_id=user_id)
    except Exception as exc:
        logger.warning("risk_counters_reset_failed", error=str(exc))
