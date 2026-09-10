"""
Coupon engine — validation, preview, atomic redemption, transfer.
See app/coupons/engine.py docstring in prior session for full spec.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    CouponAlreadyRedeemedError,
    CouponError,
    CouponExpiredError,
    CouponNotFoundError,
    CouponTransferError,
    CouponUsageLimitError,
)
from app.core.logging import get_logger
from app.core.models import Coupon, CouponRedemption

logger = get_logger(__name__)

_ZERO    = Decimal("0")
_HUNDRED = Decimal("100")


async def validate_coupon(
    db: AsyncSession,
    code: str,
    user_id: int,
    service_id: Optional[int] = None,
    category_id: Optional[int] = None,
    order_total: Decimal = _ZERO,
) -> Coupon:
    stmt = select(Coupon).where(
        Coupon.code == code.strip().upper(),
        Coupon.is_active == True,
    )
    coupon = (await db.execute(stmt)).scalar_one_or_none()
    if coupon is None:
        raise CouponNotFoundError(detail=f"Coupon {code!r} not found")

    if coupon.expires_at and coupon.expires_at < datetime.now(timezone.utc):
        raise CouponExpiredError(detail=f"Coupon {code!r} expired")

    if coupon.total_limit is not None:
        used = (await db.execute(
            select(func.count()).where(CouponRedemption.coupon_id == coupon.id)
        )).scalar_one()
        if used >= coupon.total_limit:
            raise CouponUsageLimitError(detail=f"Coupon {code!r} limit reached")

    user_used = (await db.execute(
        select(func.count()).where(
            CouponRedemption.coupon_id == coupon.id,
            CouponRedemption.user_id == user_id,
        )
    )).scalar_one()
    if user_used >= coupon.per_user_limit:
        raise CouponAlreadyRedeemedError(detail=f"User {user_id} already used {code!r}")

    if coupon.min_spend and order_total < coupon.min_spend:
        raise CouponError(
            detail=f"Order total {order_total} below min_spend {coupon.min_spend}",
            user_message=f"This coupon requires a minimum order of ₹{coupon.min_spend:.2f}.",
        )

    if coupon.service_ids and service_id is not None:
        if service_id not in coupon.service_ids:
            raise CouponError(
                detail=f"Coupon not valid for service {service_id}",
                user_message="This coupon is not valid for the selected service.",
            )

    if coupon.category_ids and category_id is not None:
        if category_id not in coupon.category_ids:
            raise CouponError(
                detail=f"Coupon not valid for category {category_id}",
                user_message="This coupon is not valid for this category.",
            )

    return coupon


def calculate_discount(coupon: Coupon, order_total: Decimal) -> Decimal:
    if coupon.discount_type == "percentage":
        raw = (order_total * coupon.discount_value / _HUNDRED).quantize(Decimal("0.01"))
    elif coupon.discount_type == "fixed":
        raw = coupon.discount_value.quantize(Decimal("0.01"))
    else:
        raw = _ZERO

    if coupon.max_discount and raw > coupon.max_discount:
        raw = coupon.max_discount

    return max(min(raw, order_total), _ZERO)


async def preview_coupon(
    db: AsyncSession,
    code: str,
    user_id: int,
    service_id: Optional[int] = None,
    order_total: Decimal = _ZERO,
) -> Decimal:
    coupon = await validate_coupon(
        db=db, code=code, user_id=user_id,
        service_id=service_id, order_total=order_total,
    )
    return calculate_discount(coupon, order_total)


async def apply_coupon(
    db: AsyncSession,
    code: str,
    user_id: int,
    order_id: str,
    order_total: Decimal,
    service_id: Optional[int] = None,
    category_id: Optional[int] = None,
    idempotency_key: Optional[str] = None,
) -> Decimal:
    idem_key = idempotency_key or f"coupon:{order_id}:{code}"

    existing = (await db.execute(
        select(CouponRedemption).where(CouponRedemption.idempotency_key == idem_key)
    )).scalar_one_or_none()
    if existing is not None:
        return existing.discount_applied

    coupon   = await validate_coupon(
        db=db, code=code, user_id=user_id,
        service_id=service_id, category_id=category_id,
        order_total=order_total,
    )
    discount = calculate_discount(coupon, order_total)

    redemption = CouponRedemption(
        coupon_id=coupon.id,
        user_id=user_id,
        order_id=order_id,
        discount_applied=discount,
        idempotency_key=idem_key,
        created_at=datetime.now(timezone.utc),
    )
    db.add(redemption)
    await db.flush()

    logger.info("coupon_redeemed", code=code, user_id=user_id, order_id=order_id, discount=str(discount))
    return discount


async def transfer_coupon(
    db: AsyncSession,
    code: str,
    from_user_id: int,
    to_user_id: int,
) -> None:
    coupon = (await db.execute(
        select(Coupon).where(
            Coupon.code == code.strip().upper(),
            Coupon.is_active == True,
            Coupon.is_transferable == True,
            Coupon.owner_user_id == from_user_id,
        )
    )).scalar_one_or_none()

    if coupon is None:
        raise CouponTransferError(
            detail=f"Coupon {code!r} not found, not owned by {from_user_id}, or not transferable",
        )

    if coupon.expires_at and coupon.expires_at < datetime.now(timezone.utc):
        raise CouponExpiredError(detail=f"Coupon {code!r} has expired")

    coupon.owner_user_id = to_user_id
    await db.flush()
    logger.info("coupon_transferred", code=code, from_user_id=from_user_id, to_user_id=to_user_id)
