"""Sell-price engine.

Hierarchy (first match wins for the sell rate):
  1. Star custom rate (user-specific, else global star for the service)
  2. Service markup
  3. Category markup
  4. Provider markup
  5. Global markup

Active resellers then receive their panel discount, floored at a 10% margin
versus provider cost. Markups may be percent, fixed paise, or both. A minimum
margin is always enforced server-side. Client-supplied prices are ignored.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.pricemath import cap_discount_to_floor, margin_floor_per_1000
from app.models import (
    DEFAULT_RESELLER_DISCOUNT_BP,
    RESELLER_MIN_MARGIN_PERCENT,
    SETTING_GLOBAL_MARKUP,
    SETTING_MIN_MARGIN,
    AppSetting,
    Category,
    Coupon,
    CouponRedemption,
    Provider,
    Reseller,
    Service,
    StarPrice,
)


def paise_to_rupees_str(paise: int) -> str:
    sign = "-" if paise < 0 else ""
    value = abs(int(paise))
    rupees, remainder = divmod(value, 100)
    return f"{sign}₹{rupees:,}.{remainder:02d}"


def rupees_to_paise(amount: float) -> int:
    return int(round(amount * 100))


@dataclass(frozen=True)
class PriceQuote:
    service_id: str
    quantity: int
    cost_per_1000_paise: int
    sell_per_1000_paise: int
    cost_paise: int
    subtotal_paise: int
    discount_paise: int
    charge_paise: int
    layer: str
    markup_percent: float
    markup_fixed_paise: int
    coupon_code: str | None
    min_qty: int
    max_qty: int
    currency: str = "INR"
    retail_per_1000_paise: int = 0
    reseller_discount_paise: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "service_id": self.service_id,
            "quantity": self.quantity,
            "cost_per_1000_paise": self.cost_per_1000_paise,
            "sell_per_1000_paise": self.sell_per_1000_paise,
            "cost_paise": self.cost_paise,
            "subtotal_paise": self.subtotal_paise,
            "discount_paise": self.discount_paise,
            "charge_paise": self.charge_paise,
            "charge_display": paise_to_rupees_str(self.charge_paise),
            "sell_per_1000_display": paise_to_rupees_str(self.sell_per_1000_paise),
            "layer": self.layer,
            "coupon_code": self.coupon_code,
            "min_qty": self.min_qty,
            "max_qty": self.max_qty,
            "reseller_discount_paise": self.reseller_discount_paise,
        }


async def _setting_float(session: AsyncSession, key: str, default: float) -> float:
    row = await session.get(AppSetting, key)
    if row is None:
        return default
    try:
        return float(row.value)
    except (TypeError, ValueError):
        return default


def _apply_markup(cost_per_1000: int, percent: float | None, fixed: int | None) -> int:
    pct = float(percent or 0.0)
    fix = int(fixed or 0)
    return int(math.ceil(cost_per_1000 * (1.0 + pct / 100.0) + fix))


def _enforce_min_margin(cost_per_1000: int, sell_per_1000: int, min_margin_percent: float) -> int:
    return max(sell_per_1000, margin_floor_per_1000(cost_per_1000, min_margin_percent))


from app.pricemath import scale_to_qty  # noqa: E402,F401  (re-exported)


def apply_reseller_rate(cost_per_1000: int, retail_per_1000: int, discount_bp: int) -> int:
    """Apply reseller discount, never dropping below a 10% margin vs cost."""
    bp = int(discount_bp or DEFAULT_RESELLER_DISCOUNT_BP)
    bp = max(0, min(bp, 9000))
    discounted = int(math.ceil(retail_per_1000 * (10_000 - bp) / 10_000.0))
    return _enforce_min_margin(cost_per_1000, discounted, RESELLER_MIN_MARGIN_PERCENT)


async def resolve_sell_rate(
    session: AsyncSession,
    service: Service,
    user_id: int | None,
    *,
    global_markup_percent: float | None = None,
    min_margin_percent: float | None = None,
) -> tuple[int, str, float, int]:
    """Return (sell_per_1000_paise, layer_name, applied_percent, applied_fixed)."""
    if global_markup_percent is None:
        global_markup_percent = await _setting_float(session, SETTING_GLOBAL_MARKUP, 25.0)
    if min_margin_percent is None:
        min_margin_percent = await _setting_float(session, SETTING_MIN_MARGIN, 5.0)

    cost = int(service.rate_per_1000_paise)

    star: StarPrice | None = None
    if user_id is not None:
        star = (
            await session.execute(
                select(StarPrice).where(
                    StarPrice.service_id == service.id,
                    StarPrice.user_id == user_id,
                    StarPrice.is_active.is_(True),
                )
            )
        ).scalar_one_or_none()
    if star is None:
        star = (
            await session.execute(
                select(StarPrice).where(
                    StarPrice.service_id == service.id,
                    StarPrice.user_id.is_(None),
                    StarPrice.is_active.is_(True),
                )
            )
        ).scalar_one_or_none()
    if star is not None:
        sell = _enforce_min_margin(cost, int(star.custom_rate_per_1000_paise), min_margin_percent)
        return sell, "star", 0.0, 0

    if service.markup_percent is not None or service.markup_fixed_paise is not None:
        sell = _apply_markup(cost, service.markup_percent, service.markup_fixed_paise)
        sell = _enforce_min_margin(cost, sell, min_margin_percent)
        return sell, "service", float(service.markup_percent or 0), int(service.markup_fixed_paise or 0)

    category = service.category
    if category is None:
        category = await session.get(Category, service.category_id)
    if category is not None and (
        category.markup_percent is not None or category.markup_fixed_paise is not None
    ):
        sell = _apply_markup(cost, category.markup_percent, category.markup_fixed_paise)
        sell = _enforce_min_margin(cost, sell, min_margin_percent)
        return sell, "category", float(category.markup_percent or 0), int(category.markup_fixed_paise or 0)

    provider = service.provider
    if provider is None:
        provider = await session.get(Provider, service.provider_id)
    if provider is not None and (
        provider.markup_percent is not None or provider.markup_fixed_paise is not None
    ):
        sell = _apply_markup(cost, provider.markup_percent, provider.markup_fixed_paise)
        sell = _enforce_min_margin(cost, sell, min_margin_percent)
        return sell, "provider", float(provider.markup_percent or 0), int(provider.markup_fixed_paise or 0)

    sell = _apply_markup(cost, global_markup_percent, 0)
    sell = _enforce_min_margin(cost, sell, min_margin_percent)
    return sell, "global", float(global_markup_percent), 0


async def apply_coupon(
    session: AsyncSession,
    *,
    code: str | None,
    user_id: int,
    subtotal_paise: int,
) -> tuple[int, str | None, Coupon | None]:
    if not code:
        return 0, None, None
    normalized = code.strip().upper()
    coupon = await session.get(Coupon, normalized)
    if coupon is None or not coupon.is_active:
        raise ValueError("Unknown or inactive coupon")
    from app.security import utcnow

    if coupon.expires_at is not None and coupon.expires_at < utcnow():
        raise ValueError("Coupon has expired")
    if coupon.min_order_paise and subtotal_paise < coupon.min_order_paise:
        raise ValueError(
            f"Coupon requires a minimum order of {paise_to_rupees_str(coupon.min_order_paise)}"
        )
    if coupon.max_uses is not None and coupon.used_count >= coupon.max_uses:
        raise ValueError("Coupon has reached its usage limit")
    used_by_user = (
        await session.execute(
            select(CouponRedemption).where(
                CouponRedemption.coupon_code == coupon.code,
                CouponRedemption.user_id == user_id,
            )
        )
    ).scalars().all()
    if coupon.per_user_limit and len(used_by_user) >= coupon.per_user_limit:
        raise ValueError("You have already used this coupon")

    if coupon.discount_type == "percent":
        discount = int(math.floor(subtotal_paise * float(coupon.value) / 100.0))
    else:
        discount = int(coupon.value)
    discount = max(0, min(discount, subtotal_paise))
    return discount, coupon.code, coupon


async def quote_order(
    session: AsyncSession,
    *,
    service_id: str,
    quantity: int,
    user_id: int | None,
    coupon_code: str | None = None,
) -> PriceQuote:
    result = await session.execute(
        select(Service)
        .options(selectinload(Service.category), selectinload(Service.provider))
        .where(Service.id == service_id)
    )
    service = result.scalar_one_or_none()
    if service is None or not service.is_active:
        raise ValueError("Service is not available")
    if quantity < service.min_qty or quantity > service.max_qty:
        raise ValueError(f"Quantity must be between {service.min_qty} and {service.max_qty}")

    sell_per_1000, layer, pct, fixed = await resolve_sell_rate(session, service, user_id)
    retail_per_1000 = sell_per_1000
    reseller_discount_paise = 0
    if user_id is not None:
        reseller = await session.get(Reseller, user_id)
        if reseller is not None and reseller.status == "active":
            sell_per_1000 = apply_reseller_rate(
                int(service.rate_per_1000_paise), retail_per_1000, int(reseller.discount_bp)
            )
            layer = f"{layer}+reseller"
    cost_paise = scale_to_qty(service.rate_per_1000_paise, quantity)
    retail_subtotal = scale_to_qty(retail_per_1000, quantity)
    subtotal = scale_to_qty(sell_per_1000, quantity)
    reseller_discount_paise = max(0, retail_subtotal - subtotal)
    discount = 0
    applied_code: str | None = None
    if coupon_code and user_id is not None:
        discount, applied_code, _ = await apply_coupon(
            session, code=coupon_code, user_id=user_id, subtotal_paise=subtotal
        )
        # A coupon must never turn an order into a loss. Resellers keep their stricter floor.
        min_margin = await _setting_float(session, SETTING_MIN_MARGIN, 5.0)
        if "+reseller" in layer:
            min_margin = max(min_margin, RESELLER_MIN_MARGIN_PERCENT)
        floor_charge = scale_to_qty(
            margin_floor_per_1000(int(service.rate_per_1000_paise), min_margin), quantity
        )
        discount = cap_discount_to_floor(subtotal, discount, floor_charge)
        if discount <= 0:
            applied_code = None  # nothing applied: do not burn the customer's coupon use
    charge = max(0, subtotal - discount)
    return PriceQuote(
        service_id=service.id,
        quantity=quantity,
        cost_per_1000_paise=int(service.rate_per_1000_paise),
        sell_per_1000_paise=sell_per_1000,
        cost_paise=cost_paise,
        subtotal_paise=subtotal,
        discount_paise=discount,
        charge_paise=charge,
        layer=layer,
        markup_percent=pct,
        markup_fixed_paise=fixed,
        coupon_code=applied_code,
        min_qty=service.min_qty,
        max_qty=service.max_qty,
        retail_per_1000_paise=retail_per_1000,
        reseller_discount_paise=reseller_discount_paise,
    )
