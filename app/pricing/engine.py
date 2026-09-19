"""
Pricing engine — 5-level hierarchy.

Resolution order (first match wins at each level):
  1. custom_price on the service → override, skip all rules
  2. service.profit_pct          → service-level markup
  3. category.profit_pct         → category-level markup
  4. provider.profit_pct         → provider-level markup
  5. global PricingRule          → platform-wide markup

After the markup:
  - Premium discount applied (if user is premium/vip)
  - Reseller discount applied (capped at min_margin_pct floor)
  - Coupon flat deduction applied (capped at min_margin_pct floor)

Currency:
  Provider rates are in provider.currency (usually USD).
  Customer prices are in INR.
  USD→INR rate: fetched from Redis cache (live) or settings fallback.

Margin floor:
  floor_price = provider_cost_inr * (1 + min_margin_pct / 100)
  Neither reseller nor coupon discount can push price below floor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import PricingError, ValidationError
from app.core.logging import get_logger
from app.core.models import PricingRule

logger = get_logger(__name__)

_ZERO    = Decimal("0")
_HUNDRED = Decimal("100")
_DEFAULT_MIN_MARGIN = Decimal("5")


@dataclass
class PriceResult:
    customer_price:     Decimal
    provider_cost_inr:  Decimal
    floor_price:        Decimal
    applied_pct:        Decimal
    custom_price_used:  bool
    reseller_capped:    bool
    coupon_capped:      bool
    premium_capped:     bool
    pricing_path:       list[str]
    currency:           str = "INR"
    standard_price:     Optional[Decimal] = None

    def to_audit_dict(self) -> dict:
        return {
            "customer_price":    str(self.customer_price),
            "provider_cost_inr": str(self.provider_cost_inr),
            "floor_price":       str(self.floor_price),
            "applied_pct":       str(self.applied_pct),
            "custom_price_used": self.custom_price_used,
            "pricing_path":      self.pricing_path,
        }


async def calculate_price(
    db: AsyncSession,
    service,
    quantity: int,
    provider_rate: Decimal,
    provider_currency: str = "USD",
    target_currency: str = "INR",
    usd_to_inr_rate: Optional[Decimal] = None,
    reseller_discount_pct: Optional[Decimal] = None,
    coupon_discount_flat: Optional[Decimal] = None,
    premium_discount_pct: Optional[Decimal] = None,
) -> PriceResult:
    """
    Calculate the customer price for a service order.

    Parameters:
      service              — Service ORM object
      quantity             — number of units ordered
      provider_rate        — cost per 1000 units in provider currency
      provider_currency    — 'USD' or 'INR'
      target_currency      — always 'INR' for customer-facing prices
      usd_to_inr_rate      — live FX rate; fetched from cache if None
      reseller_discount_pct — reseller discount off standard price
      coupon_discount_flat  — flat coupon discount in INR
      premium_discount_pct  — premium plan discount percentage

    Returns PriceResult with full audit trail in pricing_path.
    """
    if quantity <= 0:
        raise ValidationError(
            detail=f"Invalid quantity: {quantity}",
            user_message="Quantity must be greater than zero.",
        )

    pricing_path: list[str] = []

    # ── FX conversion ─────────────────────────────────────────────────────────
    if usd_to_inr_rate is None:
        try:
            from app.fx.rates import get_usd_to_inr_rate
            usd_to_inr_rate = await get_usd_to_inr_rate()
        except Exception:
            from app.core.config import settings
            usd_to_inr_rate = getattr(settings, 'usd_to_inr_rate', Decimal("83"))

    # Provider cost in INR per 1000 units
    if provider_currency.upper() == "USD":
        provider_cost_per_1000_inr = (provider_rate * usd_to_inr_rate).quantize(Decimal("0.0001"))
        pricing_path.append(f"fx: {provider_rate} USD × {usd_to_inr_rate} = {provider_cost_per_1000_inr} INR/1000")
    else:
        provider_cost_per_1000_inr = provider_rate.quantize(Decimal("0.0001"))
        pricing_path.append(f"provider_rate: {provider_cost_per_1000_inr} INR/1000")

    provider_cost_total = (provider_cost_per_1000_inr * quantity / 1000).quantize(Decimal("0.0001"))
    pricing_path.append(f"provider_cost: {provider_cost_total} INR for {quantity} units")

    # ── Custom price short-circuit ─────────────────────────────────────────────
    if service.custom_price is not None and service.custom_price > _ZERO:
        customer_per_1000 = service.custom_price
        customer_price    = (customer_per_1000 * quantity / 1000).quantize(Decimal("0.01"))
        pricing_path.append(f"custom_price: {customer_per_1000}/1000 → {customer_price}")
        min_margin_pct = _DEFAULT_MIN_MARGIN
        floor_price    = (provider_cost_total * (1 + min_margin_pct / _HUNDRED)).quantize(Decimal("0.01"))
        return PriceResult(
            customer_price=customer_price,
            provider_cost_inr=provider_cost_total,
            floor_price=floor_price,
            applied_pct=_ZERO,
            custom_price_used=True,
            reseller_capped=False,
            coupon_capped=False,
            premium_capped=False,
            pricing_path=pricing_path,
            standard_price=customer_price,
        )

    # ── Rule resolution ────────────────────────────────────────────────────────
    markup_pct, min_margin_pct, rule_scope = await _resolve_markup(db, service)
    pricing_path.append(f"markup: {markup_pct}% from {rule_scope}")

    standard_per_1000 = (provider_cost_per_1000_inr * (1 + markup_pct / _HUNDRED)).quantize(Decimal("0.0001"))
    standard_price    = (standard_per_1000 * quantity / 1000).quantize(Decimal("0.01"))
    floor_price       = (provider_cost_total * (1 + min_margin_pct / _HUNDRED)).quantize(Decimal("0.01"))
    pricing_path.append(f"standard_price: {standard_price} (floor: {floor_price})")

    customer_price = standard_price
    reseller_capped = coupon_capped = premium_capped = False

    # ── Premium discount ──────────────────────────────────────────────────────
    if premium_discount_pct and premium_discount_pct > _ZERO:
        after_prem = (customer_price * (1 - premium_discount_pct / _HUNDRED)).quantize(Decimal("0.01"))
        if after_prem < floor_price:
            customer_price = floor_price
            premium_capped = True
            pricing_path.append(f"premium_discount_pct={premium_discount_pct} capped at floor")
        else:
            customer_price = after_prem
            pricing_path.append(f"premium_discount_pct={premium_discount_pct} → {customer_price}")

    # ── Reseller discount ─────────────────────────────────────────────────────
    if reseller_discount_pct and reseller_discount_pct > _ZERO:
        after_res = (customer_price * (1 - reseller_discount_pct / _HUNDRED)).quantize(Decimal("0.01"))
        if after_res < floor_price:
            customer_price  = floor_price
            reseller_capped = True
            pricing_path.append(f"reseller_discount_pct={reseller_discount_pct} capped at floor")
        else:
            customer_price = after_res
            pricing_path.append(f"reseller_discount_pct={reseller_discount_pct} → {customer_price}")

    # ── Coupon discount ───────────────────────────────────────────────────────
    if coupon_discount_flat and coupon_discount_flat > _ZERO:
        after_coupon = (customer_price - coupon_discount_flat).quantize(Decimal("0.01"))
        if after_coupon < floor_price:
            customer_price = floor_price
            coupon_capped  = True
            pricing_path.append(f"coupon_flat={coupon_discount_flat} capped at floor")
        else:
            customer_price = after_coupon
            pricing_path.append(f"coupon_flat={coupon_discount_flat} → {customer_price}")

    return PriceResult(
        customer_price=customer_price,
        provider_cost_inr=provider_cost_total,
        floor_price=floor_price,
        applied_pct=markup_pct,
        custom_price_used=False,
        reseller_capped=reseller_capped,
        coupon_capped=coupon_capped,
        premium_capped=premium_capped,
        pricing_path=pricing_path,
        standard_price=standard_price,
    )


async def _resolve_markup(
    db: AsyncSession,
    service,
) -> tuple[Decimal, Decimal, str]:
    """
    Resolve markup percentage via the 5-level hierarchy.
    Returns (markup_pct, min_margin_pct, scope_label).
    """
    # Level 1: service.profit_pct
    if service.profit_pct is not None:
        return service.profit_pct, _DEFAULT_MIN_MARGIN, "service"

    # Level 2: category.profit_pct
    if service.category_id:
        from app.core.models import Category
        cat = await db.get(Category, service.category_id)
        if cat and cat.profit_pct is not None:
            return cat.profit_pct, _DEFAULT_MIN_MARGIN, "category"

    # Level 3: global PricingRule (most recent active rule)
    rule = (await db.execute(
        select(PricingRule).where(
            PricingRule.scope == "global",
            PricingRule.is_active == True,
        ).order_by(PricingRule.id.desc())
    )).scalar_one_or_none()

    if rule:
        return rule.value, rule.min_margin_pct, "global_rule"

    # Level 4: default
    return Decimal("20"), _DEFAULT_MIN_MARGIN, "default"


def verify_price_unchanged(
    original: Decimal,
    recalculated: Decimal,
    tolerance: Optional[Decimal] = None,
) -> bool:
    """
    Verify price hasn't changed between preview and order submission.
    Default tolerance: ₹0.01 (one paisa — handles rounding).
    """
    tol = tolerance if tolerance is not None else Decimal("0.01")
    return abs(original - recalculated) <= tol
