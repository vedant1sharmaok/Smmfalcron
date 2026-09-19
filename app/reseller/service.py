"""
Reseller platform.

Resellers get a percentage discount off the standard customer price.
The discount can never reduce the effective price below the configured
minimum margin — the margin protection floor is enforced here, not at
the order engine level, so it can't be bypassed via the reseller path.

Reseller pricing hierarchy:
  standard_price = pricing_engine.calculate_price(...)
  reseller_price = standard_price * (1 - discount_pct / 100)
  floor          = provider_cost * (1 + min_margin_pct / 100)
  final          = max(reseller_price, floor)

Reseller users are regular users with a ReselllerProfile linked.
They place orders through the same order engine — the only difference
is the price they pay and the source field on the order.

Reseller API access uses the same own-SMM-API surface as API customers
but with reseller-specific pricing applied automatically.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase

from app.core.exceptions import AuthorizationError, ValidationError, WalletError
from app.core.logging import get_logger

logger = get_logger(__name__)

_ZERO    = Decimal("0")
_HUNDRED = Decimal("100")


# ── ORM model ─────────────────────────────────────────────────────────────────
# Defined here for Phase 13; added to migration 0002.

from app.core.database import Base
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text, BigInteger
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from datetime import datetime


class ResellerProfile(Base):
    """
    Linked to a User row.  One reseller profile per user.
    Resellers are identified by their telegram_id / user_id.
    """
    __tablename__ = "reseller_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"),
                                          unique=True, nullable=False)
    plan_name: Mapped[str] = mapped_column(String(64), nullable=False, default="standard")
    discount_pct: Mapped[Decimal] = mapped_column(Numeric(8, 4), nullable=False,
                                                   default=Decimal("0"))
    min_margin_pct: Mapped[Decimal] = mapped_column(Numeric(8, 4), nullable=False,
                                                     default=Decimal("5"))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    monthly_order_limit: Mapped[int | None] = mapped_column(Integer)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    extra_config: Mapped[dict | None] = mapped_column(JSONB)


# ── Service layer ──────────────────────────────────────────────────────────────

@dataclass
class ResellerPriceResult:
    standard_price: Decimal
    discount_pct:   Decimal
    discount_amount: Decimal
    floor_price:    Decimal
    final_price:    Decimal
    was_capped:     bool
    currency:       str


async def get_reseller_profile(
    db: AsyncSession,
    user_id: int,
) -> ResellerProfile | None:
    """Return the reseller profile for a user, or None if not a reseller."""
    stmt = select(ResellerProfile).where(
        ResellerProfile.user_id == user_id,
        ResellerProfile.is_active == True,
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def calculate_reseller_price(
    standard_price: Decimal,
    provider_cost:  Decimal,
    profile:        ResellerProfile,
    currency:       str = "INR",
) -> ResellerPriceResult:
    """
    Apply reseller discount with margin-floor protection.

    The discount percentage comes from the reseller's plan.
    The floor is provider_cost × (1 + min_margin_pct / 100).
    If the discounted price falls below the floor, the floor is used instead.
    """
    if standard_price <= _ZERO:
        raise ValidationError(
            detail="standard_price must be positive",
            user_message="Pricing error. Please try again.",
        )

    discount_pct    = profile.discount_pct or _ZERO
    discount_amount = (standard_price * discount_pct / _HUNDRED).quantize(
        Decimal("0.00000001")
    )
    discounted      = standard_price - discount_amount

    # Margin floor: provider_cost × (1 + min_margin / 100)
    min_margin_pct  = profile.min_margin_pct or _ZERO
    floor_price     = (provider_cost * (1 + min_margin_pct / _HUNDRED)).quantize(
        Decimal("0.01")
    )

    was_capped   = discounted < floor_price
    final_price  = max(discounted, floor_price).quantize(Decimal("0.01"))

    if was_capped:
        logger.warning(
            "reseller_discount_capped_at_margin_floor",
            user_id=getattr(profile, 'user_id', 'unknown'),
            standard_price=str(standard_price),
            discounted=str(discounted),
            floor_price=str(floor_price),
            discount_pct=str(discount_pct),
        )

    return ResellerPriceResult(
        standard_price  = standard_price,
        discount_pct    = discount_pct,
        discount_amount = standard_price - final_price,
        floor_price     = floor_price,
        final_price     = final_price,
        was_capped      = was_capped,
        currency        = currency,
    )


async def create_reseller_profile(
    db: AsyncSession,
    *,
    user_id:              int,
    plan_name:            str       = "standard",
    discount_pct:         Decimal   = _ZERO,
    min_margin_pct:       Decimal   = Decimal("5"),
    monthly_order_limit:  int | None = None,
    actor_id:             int,
) -> ResellerProfile:
    """
    Create a new reseller profile for a user.
    The user must already exist.  Raises if a profile already exists.
    """
    # Check for existing profile.
    existing = await get_reseller_profile(db, user_id)
    if existing is not None:
        raise ValidationError(
            detail=f"User {user_id} already has a reseller profile",
            user_message="This user is already a reseller.",
        )

    if discount_pct < _ZERO or discount_pct >= _HUNDRED:
        raise ValidationError(
            detail=f"discount_pct must be 0–99.9999, got {discount_pct}",
            user_message="Invalid discount percentage.",
        )
    if min_margin_pct < _ZERO:
        raise ValidationError(
            detail="min_margin_pct must be non-negative",
            user_message="Invalid minimum margin.",
        )

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)

    profile = ResellerProfile(
        user_id             = user_id,
        plan_name           = plan_name,
        discount_pct        = discount_pct,
        min_margin_pct      = min_margin_pct,
        is_active           = True,
        monthly_order_limit = monthly_order_limit,
        created_at          = now,
        updated_at          = now,
    )
    db.add(profile)
    await db.flush()

    logger.info(
        "reseller_profile_created",
        user_id=user_id,
        plan_name=plan_name,
        discount_pct=str(discount_pct),
        actor_id=actor_id,
    )
    return profile


async def update_reseller_profile(
    db: AsyncSession,
    user_id: int,
    updates: dict[str, Any],
    actor_id: int,
) -> ResellerProfile:
    """Update an existing reseller profile."""
    profile = await get_reseller_profile(db, user_id)
    if profile is None:
        raise ValidationError(
            detail=f"No reseller profile found for user {user_id}",
            user_message="Reseller profile not found.",
        )

    _ALLOWED = {
        "plan_name", "discount_pct", "min_margin_pct",
        "is_active", "monthly_order_limit", "notes",
    }
    invalid = set(updates) - _ALLOWED
    if invalid:
        raise ValidationError(
            detail=f"Invalid update fields: {invalid}",
            user_message="Invalid reseller update.",
        )

    # Validate discount_pct if being updated.
    if "discount_pct" in updates:
        pct = Decimal(str(updates["discount_pct"]))
        if pct < _ZERO or pct >= _HUNDRED:
            raise ValidationError(
                detail=f"discount_pct must be 0–99.9999, got {pct}",
                user_message="Invalid discount percentage.",
            )

    from datetime import datetime, timezone
    for k, v in updates.items():
        setattr(profile, k, v)
    profile.updated_at = datetime.now(timezone.utc)

    await db.flush()
    logger.info("reseller_profile_updated", user_id=user_id, actor_id=actor_id,
                fields=list(updates.keys()))
    return profile


async def suspend_reseller(db: AsyncSession, user_id: int, actor_id: int) -> None:
    """Suspend a reseller account."""
    profile = await get_reseller_profile(db, user_id)
    if profile is None:
        raise ValidationError(detail=f"No reseller for user {user_id}",
                              user_message="Reseller not found.")
    profile.is_active = False
    await db.flush()
    logger.info("reseller_suspended", user_id=user_id, actor_id=actor_id)
