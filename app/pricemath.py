"""Pure price/discount arithmetic (stdlib only) so it can be unit-tested in isolation."""

from __future__ import annotations

import math


def margin_floor_per_1000(cost_per_1000: int, min_margin_percent: float) -> int:
    """Lowest permitted sell rate per 1,000: cost plus the minimum margin (never below cost)."""
    floor = int(math.ceil(cost_per_1000 * (1.0 + max(min_margin_percent, 0.0) / 100.0)))
    return max(floor, cost_per_1000)


def scale_to_qty(per_1000_paise: int, quantity: int) -> int:
    if quantity <= 0:
        return 0
    return int(math.ceil(per_1000_paise * quantity / 1000.0))


def cap_discount_to_floor(subtotal_paise: int, discount_paise: int, floor_paise: int) -> int:
    """Largest discount <= requested that keeps the charge at or above the margin floor.

    A coupon must never turn an order into a loss: charge = subtotal - discount >= floor.
    """
    if discount_paise <= 0 or subtotal_paise <= 0:
        return 0
    headroom = subtotal_paise - max(int(floor_paise), 0)
    if headroom <= 0:
        return 0
    return min(int(discount_paise), headroom, subtotal_paise)
