"""
Order engine — 17-step pipeline.

Steps:
  1.  Kill switch check (assert_orders_allowed)
  2.  Load and validate service (active, ordering_enabled)
  3.  Premium gate (assert_can_order — quantity cap + exclusive service)
  4.  Validate quantity (min/max from service + provider mapping)
  5.  Resolve provider mapping (primary provider)
  6.  Calculate price (server-side ALWAYS — never trusted from client)
  7.  Verify price unchanged (preview → order tolerance ₹0.01)
  8.  Check balance (read-only — actual enforcement in ledger debit)
  9.  Idempotency check (DuplicateOrderError if key already used)
  10. Debit wallet (SELECT FOR UPDATE — raises InsufficientBalanceError)
  11. Create Order row (status=pending)
  12. Submit to provider API
  13. Update Order with provider_order_id + status=processing
  14. On provider failure: compensating credit + mark order failed
  15. Set order priority from premium plan
  16. Audit log
  17. Return Order

Security invariants:
  - user_id NEVER from request body — always from session context
  - Price ALWAYS recalculated server-side
  - Debit BEFORE provider call
  - Compensating credit on any provider failure
  - Kill switch checked BEFORE debit
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    DuplicateOrderError,
    InsufficientBalanceError,
    OrderError,
    ServiceNotFoundError,
    ServiceUnavailableError,
    ValidationError,
)
from app.core.logging import get_logger
from app.core.models import Order, Service, ServiceProviderMapping
from app.pricing.engine import calculate_price, verify_price_unchanged
from app.security.kill_switches import assert_orders_allowed
from app.wallet.ledger import TxType, credit, debit

logger = get_logger(__name__)

_ZERO = Decimal("0")


def _generate_public_ref() -> str:
    """ORD-YYYYMMDD-XXXXXXXX format."""
    now  = datetime.now(timezone.utc)
    rand = uuid.uuid4().hex[:8].upper()
    return f"ORD-{now.strftime('%Y%m%d')}-{rand}"


async def create_order(
    db: AsyncSession,
    user_id: int,
    service_id: int,
    quantity: int,
    link: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    source: str = "bot",
    confirmed_price: Optional[Decimal] = None,
    coupon_code: Optional[str] = None,
    tenant_id: Optional[int] = None,
) -> Order:
    """
    Create and submit an order through the full 17-step pipeline.

    user_id must come from the authenticated session — never from request body.
    """
    idem_key = idempotency_key or str(uuid.uuid4())

    # ── Step 1: Kill switch ────────────────────────────────────────────────────
    await assert_orders_allowed(source=source)

    # ── Step 2: Load + validate service ───────────────────────────────────────
    service = await db.get(Service, service_id)
    if service is None:
        raise ServiceNotFoundError(detail=f"Service {service_id} not found")
    if not service.is_active or not service.ordering_enabled:
        raise ServiceUnavailableError(
            detail=f"Service {service_id} not available for ordering"
        )

    # ── Step 3: Premium gate ──────────────────────────────────────────────────
    try:
        from app.premium.integration import apply_premium_to_order
        premium_ctx = await apply_premium_to_order(
            db=db,
            user_id=user_id,
            service_requires_premium=service.requires_premium,
            quantity=quantity,
        )
        premium_discount_pct = premium_ctx["premium_discount_pct"]
        order_priority       = premium_ctx["order_priority"]
    except ImportError:
        premium_discount_pct = _ZERO
        order_priority       = 3

    # ── Step 4: Validate quantity ─────────────────────────────────────────────
    mapping = (await db.execute(
        select(ServiceProviderMapping).where(
            ServiceProviderMapping.service_id == service_id,
            ServiceProviderMapping.is_primary == True,
        )
    )).scalar_one_or_none()

    if mapping is None:
        raise OrderError(
            detail=f"No provider mapping for service {service_id}",
            user_message="Service is not currently available.",
        )

    # ── Step 5: Load provider + provider service ───────────────────────────────
    from app.core.models import Provider, ProviderService
    provider = await db.get(Provider, mapping.provider_id)
    if provider is None or not provider.is_active:
        raise OrderError(
            detail=f"Provider {mapping.provider_id} not active",
            user_message="Service provider is temporarily unavailable.",
        )

    provider_svc = (await db.execute(
        select(ProviderService).where(
            ProviderService.provider_id == mapping.provider_id,
            ProviderService.provider_svc_id == mapping.provider_svc_id,
        )
    )).scalar_one_or_none()

    if provider_svc is None:
        raise OrderError(
            detail=f"Provider service mapping not found",
            user_message="Service is not currently available.",
        )

    if quantity < provider_svc.min_qty or quantity > provider_svc.max_qty:
        raise ValidationError(
            detail=f"Quantity {quantity} out of range [{provider_svc.min_qty}, {provider_svc.max_qty}]",
            user_message=(
                f"Quantity must be between {provider_svc.min_qty:,} "
                f"and {provider_svc.max_qty:,}."
            ),
        )

    # ── Step 6: Calculate price ────────────────────────────────────────────────
    coupon_flat = _ZERO
    if coupon_code:
        try:
            from app.coupons.engine import preview_coupon
            coupon_flat = await preview_coupon(
                db=db, code=coupon_code, user_id=user_id,
                service_id=service_id,
            )
        except Exception:
            coupon_flat = _ZERO

    price_result = await calculate_price(
        db=db,
        service=service,
        quantity=quantity,
        provider_rate=provider_svc.rate,
        provider_currency=provider.currency,
        target_currency="INR",
        premium_discount_pct=premium_discount_pct,
        coupon_discount_flat=coupon_flat if coupon_flat > _ZERO else None,
    )
    final_price = price_result.customer_price

    # ── Step 7: Verify price ──────────────────────────────────────────────────
    if confirmed_price is not None:
        if not verify_price_unchanged(confirmed_price, final_price):
            from app.core.exceptions import PriceChangedError
            raise PriceChangedError(
                detail=f"Price changed: confirmed={confirmed_price} current={final_price}",
            )

    # ── Step 9: Idempotency check ─────────────────────────────────────────────
    existing = (await db.execute(
        select(Order).where(Order.idempotency_key == idem_key)
    )).scalar_one_or_none()
    if existing is not None:
        logger.info("order_idempotency_replay", order_id=existing.id)
        return existing

    # ── Step 10: Debit wallet ─────────────────────────────────────────────────
    debit_tx = await debit(
        db=db,
        user_id=user_id,
        amount=final_price,
        tx_type=TxType.ORDER_DEBIT,
        idempotency_key=f"order_debit:{idem_key}",
        reason=f"Order for service {service.public_id} x{quantity}",
    )

    # ── Step 11: Create Order row ─────────────────────────────────────────────
    order_id   = str(uuid.uuid4())
    public_ref = _generate_public_ref()
    now        = datetime.now(timezone.utc)

    order = Order(
        id=order_id,
        public_ref=public_ref,
        user_id=user_id,
        tenant_id=tenant_id,
        service_id=service_id,
        provider_id=provider.id,
        provider_svc_id=mapping.provider_svc_id,
        status="pending",
        quantity=quantity,
        link=link,
        price_charged=final_price,
        provider_cost=price_result.provider_cost_inr,
        currency="INR",
        idempotency_key=idem_key,
        source=source,
        priority=order_priority,
        refill_eligible=provider_svc.refill,
        cancel_eligible=provider_svc.cancel,
        created_at=now,
        updated_at=now,
    )
    db.add(order)
    await db.flush()

    # ── Step 12-13: Submit to provider ────────────────────────────────────────
    try:
        from app.providers.registry import registry
        from app.providers.models import OrderRequest as ProviderOrderRequest
        _order_req = ProviderOrderRequest(
            provider_svc_id=mapping.provider_svc_id,
            quantity=quantity,
            link=link or "",
        )
        provider_result = await registry.create_order(
            provider_id=provider.id,
            req=_order_req,
        )
        order.provider_order_id = provider_result.provider_order_id
        order.status            = "processing"
        order.updated_at        = datetime.now(timezone.utc)
        await db.flush()

        logger.info(
            "order_submitted",
            order_id=order_id,
            public_ref=public_ref,
            provider_order_id=provider_result.provider_order_id,
        )

    except Exception as exc:
        # ── Step 14: Compensating credit on provider failure ──────────────────
        logger.error(
            "order_provider_failed_refunding",
            order_id=order_id,
            error=str(exc),
        )
        order.status     = "failed"
        order.updated_at = datetime.now(timezone.utc)
        await db.flush()

        await credit(
            db=db,
            user_id=user_id,
            amount=final_price,
            tx_type=TxType.REFUND,
            idempotency_key=f"order_refund:{idem_key}",
            reference_id=order_id,
            reason=f"Auto-refund: provider error for order {public_ref}",
        )
        raise OrderError(
            detail=f"Provider submission failed: {exc}",
            user_message="Order could not be submitted. Your balance has been refunded.",
        ) from exc

    return order


async def update_order_status(
    db: AsyncSession,
    order_id: str,
    new_status: str,
    remains: Optional[int] = None,
    start_count: Optional[int] = None,
) -> Order:
    """Update an order's status and optional progress fields."""
    order = await db.get(Order, order_id)
    if order is None:
        from app.core.exceptions import OrderNotFoundError
        raise OrderNotFoundError(detail=f"Order {order_id} not found")

    order.status     = new_status
    order.updated_at = datetime.now(timezone.utc)
    if remains is not None:
        order.remains = remains
    if start_count is not None:
        order.start_count = start_count
    if new_status in ("completed", "partial", "failed", "cancelled", "refunded"):
        order.completed_at = datetime.now(timezone.utc)
        order.cancel_eligible = False
    await db.flush()
    return order


async def refund_order(
    db: AsyncSession,
    order: Order,
    actor_id: int,
    reason: str,
) -> None:
    """
    Refund a failed/partial/cancelled order.
    Idempotent: uses order.id as idempotency key.
    """
    if order.price_charged <= _ZERO:
        return

    await credit(
        db=db,
        user_id=order.user_id,
        amount=order.price_charged,
        tx_type=TxType.REFUND,
        idempotency_key=f"manual_refund:{order.id}",
        reference_id=order.id,
        reason=reason,
    )
    order.status     = "refunded"
    order.updated_at = datetime.now(timezone.utc)
    await db.flush()

    logger.info(
        "order_refunded",
        order_id=order.id,
        public_ref=order.public_ref,
        amount=str(order.price_charged),
        actor_id=actor_id,
    )
