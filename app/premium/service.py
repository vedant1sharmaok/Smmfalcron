"""
Premium system — Phase 16.

Premium users get:
  - Higher order limits (quantity caps)
  - Lower pricing (premium_discount_pct off standard prices)
  - Priority queue for order processing
  - Exclusive services (services with requires_premium=True)
  - Higher API rate limits
  - Dedicated support flag

Plans:
  basic    — free, default for all users
  premium  — paid, monthly or annual subscription
  vip      — invitation only or high-spend threshold, lifetime or annual

Plan records live in the user_premiums table:
  user_id, plan, status (active|expired|cancelled), starts_at, expires_at,
  payment_ref, auto_renew, created_at

Logic:
  - is_premium(user_id) checks the DB — single source of truth
  - Premium discount is applied AFTER the standard pricing rules
  - The pricing engine calls get_premium_discount_pct(user_id) before
    applying the reseller/coupon layer
  - Expired plans are auto-downgraded by the daily expiry worker job

Benefits by plan:
  basic:   max_qty=10000,  discount=0%,  rate_limit=60/min,   priority=3
  premium: max_qty=50000,  discount=10%, rate_limit=120/min,  priority=2
  vip:     max_qty=200000, discount=20%, rate_limit=300/min,  priority=1
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import PremiumError, ValidationError
from app.core.logging import get_logger
from app.core.models import User, UserPremium

logger = get_logger(__name__)

PlanName = Literal["basic", "premium", "vip"]

# ── Plan definitions ───────────────────────────────────────────────────────────

PLAN_BENEFITS: dict[str, dict] = {
    "basic": {
        "max_qty":        10_000,
        "discount_pct":   Decimal("0"),
        "rate_limit_rpm": 60,
        "priority":       3,
        "exclusive_svcs": False,
    },
    "premium": {
        "max_qty":        50_000,
        "discount_pct":   Decimal("10"),
        "rate_limit_rpm": 120,
        "priority":       2,
        "exclusive_svcs": True,
    },
    "vip": {
        "max_qty":        200_000,
        "discount_pct":   Decimal("20"),
        "rate_limit_rpm": 300,
        "priority":       1,
        "exclusive_svcs": True,
    },
}

# Subscription durations in days
_PLAN_DURATIONS: dict[str, dict[str, int]] = {
    "premium": {"monthly": 30, "annual": 365},
    "vip":     {"monthly": 30, "annual": 365},
}


# ── Read operations ────────────────────────────────────────────────────────────

async def get_active_premium(
    db: AsyncSession, user_id: int
) -> UserPremium | None:
    """
    Return the active UserPremium record for a user, or None if basic.
    Checks expiry — returns None if the subscription has lapsed.
    """
    record = (await db.execute(
        select(UserPremium).where(
            UserPremium.user_id == user_id,
            UserPremium.status == "active",
        )
    )).scalar_one_or_none()

    if record is None:
        return None

    # Check if subscription has expired since last sync
    if record.expires_at and record.expires_at < datetime.now(timezone.utc):
        await _expire_premium(db, record)
        return None

    return record


async def get_plan(db: AsyncSession, user_id: int) -> PlanName:
    """Return the user's current plan name. Always returns 'basic' if no active premium."""
    record = await get_active_premium(db, user_id)
    return record.plan if record else "basic"  # type: ignore[return-value]


async def get_benefits(db: AsyncSession, user_id: int) -> dict:
    """Return the full benefits dict for a user's current plan."""
    plan = await get_plan(db, user_id)
    return PLAN_BENEFITS[plan]


async def get_premium_discount_pct(db: AsyncSession, user_id: int) -> Decimal:
    """
    Return the premium discount percentage for a user.
    Called by the pricing engine before applying reseller/coupon discounts.
    Returns Decimal("0") for basic users.
    """
    benefits = await get_benefits(db, user_id)
    return benefits["discount_pct"]


async def is_premium(db: AsyncSession, user_id: int) -> bool:
    """Return True if the user has an active paid subscription."""
    return (await get_active_premium(db, user_id)) is not None


async def can_access_exclusive_service(
    db: AsyncSession, user_id: int
) -> bool:
    """Return True if the user's plan grants access to exclusive services."""
    benefits = await get_benefits(db, user_id)
    return benefits["exclusive_svcs"]


async def get_max_quantity(db: AsyncSession, user_id: int) -> int:
    """Return the maximum order quantity for a user's current plan."""
    benefits = await get_benefits(db, user_id)
    return benefits["max_qty"]


# ── Write operations ───────────────────────────────────────────────────────────

async def grant_premium(
    db: AsyncSession,
    user_id: int,
    plan: PlanName,
    duration: Literal["monthly", "annual"] = "monthly",
    payment_ref: str | None = None,
    auto_renew: bool = True,
    actor_id: int | None = None,
) -> UserPremium:
    """
    Grant a premium subscription to a user.

    If the user already has an active subscription:
      - Same plan: extends the expiry by the duration
      - Upgrade (basic→premium, premium→vip): replaces with new plan immediately
      - Downgrade: schedules the new plan to start at expiry of current

    Returns the active UserPremium record.
    """
    if plan == "basic":
        raise ValidationError(
            detail="Cannot grant 'basic' plan — that is the default",
            user_message="Basic is the default plan.",
        )

    now = datetime.now(timezone.utc)
    duration_days = _PLAN_DURATIONS[plan][duration]
    expires_at    = now + timedelta(days=duration_days)

    # Check for existing active subscription
    existing = await get_active_premium(db, user_id)

    if existing is not None:
        existing_rank = _plan_rank(existing.plan)
        new_rank      = _plan_rank(plan)

        if existing.plan == plan:
            # Extend — add duration to current expiry
            base = max(existing.expires_at, now) if existing.expires_at else now
            existing.expires_at = base + timedelta(days=duration_days)
            existing.auto_renew = auto_renew
            if payment_ref:
                existing.payment_ref = payment_ref
            await db.flush()
            logger.info("premium_extended", user_id=user_id, plan=plan, expires_at=existing.expires_at.isoformat())
            return existing

        elif new_rank > existing_rank:
            # Upgrade — cancel existing, create new
            existing.status     = "cancelled"
            existing.cancelled_at = now

    # Create new subscription
    record = UserPremium(
        user_id=user_id,
        plan=plan,
        status="active",
        starts_at=now,
        expires_at=expires_at,
        payment_ref=payment_ref,
        auto_renew=auto_renew,
        created_at=now,
    )
    db.add(record)

    # Update the user's is_premium flag
    await db.execute(
        update(User).where(User.id == user_id).values(is_premium=True)
    )
    await db.flush()

    logger.info(
        "premium_granted",
        user_id=user_id,
        plan=plan,
        duration=duration,
        expires_at=expires_at.isoformat(),
        actor_id=actor_id,
    )
    return record


async def revoke_premium(
    db: AsyncSession,
    user_id: int,
    reason: str,
    actor_id: int,
    immediate: bool = True,
) -> None:
    """
    Revoke a user's premium subscription.
    immediate=True: cancels now and resets is_premium.
    immediate=False: lets the subscription run to expiry (then not renewed).
    """
    record = await get_active_premium(db, user_id)
    if record is None:
        raise PremiumError(
            detail=f"User {user_id} has no active premium",
            user_message="User does not have an active premium subscription.",
        )

    if immediate:
        record.status       = "cancelled"
        record.cancelled_at = datetime.now(timezone.utc)
        await db.execute(
            update(User).where(User.id == user_id).values(is_premium=False)
        )
    else:
        record.auto_renew = False

    await db.flush()
    logger.info(
        "premium_revoked",
        user_id=user_id,
        immediate=immediate,
        reason=reason,
        actor_id=actor_id,
    )


async def expire_all_lapsed(db: AsyncSession) -> int:
    """
    Find all active subscriptions that have passed their expiry date
    and mark them as expired. Called by the daily worker job.
    Returns the count of expired subscriptions.
    """
    now = datetime.now(timezone.utc)
    lapsed = list((await db.execute(
        select(UserPremium).where(
            UserPremium.status == "active",
            UserPremium.expires_at < now,
        )
    )).scalars().all())

    for record in lapsed:
        await _expire_premium(db, record)

    await db.flush()

    logger.info("premium_expiry_run", expired_count=len(lapsed))
    return len(lapsed)


# ── Feature gate ───────────────────────────────────────────────────────────────

async def assert_can_order(
    db: AsyncSession,
    user_id: int,
    quantity: int,
    service_requires_premium: bool = False,
) -> None:
    """
    Check that a user is allowed to place an order with this quantity.

    Raises PremiumError if:
      - quantity > plan max_qty
      - service requires premium and user is basic

    Called by the order engine before pricing.
    """
    benefits = await get_benefits(db, user_id)
    max_qty  = benefits["max_qty"]

    if quantity > max_qty:
        plan = await get_plan(db, user_id)
        raise PremiumError(
            detail=f"User {user_id} plan={plan} max_qty={max_qty} quantity={quantity}",
            user_message=(
                f"Your plan allows a maximum of {max_qty:,} units per order. "
                f"Upgrade to Premium for higher limits."
            ),
        )

    if service_requires_premium and not await is_premium(db, user_id):
        raise PremiumError(
            detail=f"User {user_id} is basic but service requires premium",
            user_message="This service is available to Premium members only.",
        )


# ── Internal helpers ───────────────────────────────────────────────────────────

def _plan_rank(plan: str) -> int:
    return {"basic": 0, "premium": 1, "vip": 2}.get(plan, 0)


async def _expire_premium(db: AsyncSession, record: UserPremium) -> None:
    """Mark a single subscription as expired and downgrade the user."""
    record.status     = "expired"
    record.expired_at = datetime.now(timezone.utc)
    # Reset user's is_premium flag
    await db.execute(
        update(User).where(User.id == record.user_id).values(is_premium=False)
    )
    logger.info(
        "premium_expired",
        user_id=record.user_id,
        plan=record.plan,
        expired_at=record.expired_at.isoformat(),
    )
