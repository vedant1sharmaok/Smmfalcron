"""
Premium integration with the order engine.

This module documents and implements the integration points between
the premium system and the existing order engine pipeline.

Integration points in the 17-step order pipeline:

  Step 2:  Kill switch check          ← already in engine
  Step 3:  Validate service active    ← already in engine
  Step 3b: [NEW] Premium service gate — assert_can_order() checks
           service.requires_premium against user's plan
  Step 4:  Validate quantity (min/max)← already in engine
  Step 4b: [NEW] Premium qty gate    — assert_can_order() checks
           quantity against plan max_qty (overrides service max)
  Step 5:  Calculate price            ← already in engine
  Step 5b: [NEW] Apply premium disc  — get_premium_discount_pct() called
           inside calculate_price() after standard markup, before
           reseller/coupon layer
  Step 6:  Verify price unchanged     ← already in engine
  Step 7:  Check balance              ← already in engine
  Step 8:  Idempotency check          ← already in engine
  Step 9:  Debit wallet               ← already in engine
  Step 10: Create order row           ← already in engine
  Step 11: Submit to provider         ← already in engine
  Step 12: [NEW] Priority queue tag  — order.priority = plan.priority
           for order monitor polling order

Patch summary:
  - calculate_price() gets premium_discount_pct parameter
  - create_order() calls assert_can_order() before pricing
  - Order row gets priority field from plan benefits
  - Worker polls higher-priority orders first

None of these require changing the existing function signatures —
they add optional parameters with sensible defaults.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.core.logging import get_logger
from app.premium.service import (
    PLAN_BENEFITS,
    assert_can_order,
    get_premium_discount_pct,
)

logger = get_logger(__name__)


async def apply_premium_to_order(
    db,
    user_id: int,
    service_requires_premium: bool,
    quantity: int,
) -> dict[str, Any]:
    """
    Run all premium checks for an incoming order.
    Call this BEFORE pricing, AFTER kill switch checks.

    Returns dict with:
      premium_discount_pct: Decimal — extra discount for premium users
      order_priority:       int     — queue priority (1=highest, 3=lowest)
      plan:                 str     — user's current plan name

    Raises PremiumError if the order is not permitted for this user's plan.
    """
    # Gate 1: exclusive service + quantity check
    await assert_can_order(
        db=db,
        user_id=user_id,
        quantity=quantity,
        service_requires_premium=service_requires_premium,
    )

    # Gate 2: get discount for pricing engine
    premium_discount_pct = await get_premium_discount_pct(db, user_id)

    # Gate 3: get order priority for the worker queue
    from app.premium.service import get_plan
    plan = await get_plan(db, user_id)
    order_priority = PLAN_BENEFITS[plan]["priority"]

    return {
        "premium_discount_pct": premium_discount_pct,
        "order_priority":       order_priority,
        "plan":                 plan,
    }


def apply_premium_discount(
    base_price: Decimal,
    premium_discount_pct: Decimal,
    floor_price: Decimal,
) -> tuple[Decimal, bool]:
    """
    Apply the premium discount to a base price, floored at floor_price.

    Returns (final_price, was_capped).

    The premium discount is applied AFTER the standard markup rule
    and BEFORE the reseller/coupon layer.

    This matches the pricing hierarchy:
      provider_cost
        + standard_markup (global/category/service rule)
        - premium_discount (plan benefit)
        - reseller_discount (capped at min_margin)
        - coupon_discount (capped at min_margin)
    """
    if premium_discount_pct <= Decimal("0"):
        return base_price, False

    discounted = (base_price * (1 - premium_discount_pct / 100)).quantize(Decimal("0.01"))

    if discounted < floor_price:
        return floor_price, True

    return discounted, False


# ── Pricing engine patch ───────────────────────────────────────────────────────

"""
Patch for app/pricing/engine.py — add these lines to calculate_price():

    # After standard markup, before reseller/coupon:
    if premium_discount_pct and premium_discount_pct > Decimal("0"):
        after_premium, premium_capped = apply_premium_discount(
            base_price=customer_price,
            premium_discount_pct=premium_discount_pct,
            floor_price=floor_price,
        )
        if premium_capped:
            pricing_path.append(f"premium_discount_pct={premium_discount_pct} capped at floor")
        else:
            pricing_path.append(f"premium_discount_pct={premium_discount_pct}")
        customer_price = after_premium

And add premium_discount_pct: Decimal = Decimal("0") to the function signature.
"""


# ── Order engine patch ─────────────────────────────────────────────────────────

"""
Patch for app/orders/engine.py — add after kill switch check:

    # Step 3b: Premium gate
    from app.premium.integration import apply_premium_to_order
    premium_ctx = await apply_premium_to_order(
        db=db,
        user_id=user_id,
        service_requires_premium=service.requires_premium,
        quantity=quantity,
    )

    # Step 5: Price calculation (pass premium discount)
    price_result = await calculate_price(
        db=db,
        service=service,
        quantity=quantity,
        provider_rate=provider_svc.rate,
        provider_currency=provider.currency,
        target_currency="INR",
        premium_discount_pct=premium_ctx["premium_discount_pct"],
        ...
    )

    # Step 10: Create order row (set priority)
    order = Order(
        ...
        priority=premium_ctx["order_priority"],
        ...
    )
"""
