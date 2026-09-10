"""
Own SMM API — API-as-a-service product.

Exposes a standard SMM panel API interface backed by the platform's
canonical service catalog. Provider routing is invisible to API customers.

Endpoint: POST /api/v1
Parameters: key={api_key}&action={action}&[action-specific params]

Supported actions:
  services      — full service list (canonical IDs only)
  add           — create order
  status        — single order status
  multi_status  — batch order status (comma-separated IDs, max 100)
  refill        — request refill
  refill_status — single refill status
  multi_refill  — batch refill status
  cancel        — cancel order
  balance       — API wallet balance

Authentication:
  SHA-256 hash of raw API key compared against api_customers.api_key_hash.
  Never bcrypt (too slow for high-frequency API callers).
  Raw key is shown exactly once at creation; only hash stored.

Rate limiting:
  Per api_key_hash, per-minute sliding window from api_customer.rate_limit_rpm.
  Exceeding the limit returns {"error": "Rate limit exceeded"}.

Financial:
  Every successful order debits the API wallet (not the user wallet).
  Insufficient API balance returns {"error": "Insufficient balance"}.

Security:
  Provider IDs, provider names, provider service IDs NEVER in responses.
  API key never logged, never returned after creation.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import hash_api_key
from app.core.database import get_db
from app.core.exceptions import (
    ApiBalanceError,
    ApiKeyError,
    DuplicateOrderError,
    InsufficientBalanceError,
    ProviderError,
    ServiceNotFoundError,
)
from app.core.logging import get_logger
from app.core.models import ApiCustomer, ApiWallet, ApiWalletTransaction, Order, Service
from app.orders.engine import create_order
from app.providers.registry import registry
from app.security.kill_switches import assert_api_allowed, assert_orders_allowed
from app.security.rate_limiter import check_api_key_rate_limit
from app.services.catalog import get_service_by_public_id, list_active_services

logger = get_logger(__name__)
router = APIRouter(prefix="/api/v1", tags=["own_api"])

_USD_TO_INR = Decimal("83")


# ── Auth helper ────────────────────────────────────────────────────────────────

async def _authenticate_api_key(
    db: AsyncSession, raw_key: str
) -> ApiCustomer:
    """
    Authenticate an API key.
    SHA-256 hash of the raw key is compared against the stored hash.
    Returns the ApiCustomer on success; raises ApiKeyError on failure.
    """
    if not raw_key or not raw_key.startswith("smm_"):
        raise ApiKeyError(detail="Invalid API key format")

    key_hash = hash_api_key(raw_key)
    stmt = select(ApiCustomer).where(
        ApiCustomer.api_key_hash == key_hash,
        ApiCustomer.is_active == True,
    )
    customer = (await db.execute(stmt)).scalar_one_or_none()
    if customer is None:
        raise ApiKeyError(detail="API key not found or inactive")
    return customer


async def _check_api_rate_limit(customer: ApiCustomer, key_hash: str) -> None:
    """Check per-key rate limit. Raises if exceeded."""
    result = await check_api_key_rate_limit(
        key_hash=key_hash,
        limit_rpm=customer.rate_limit_rpm,
    )
    if not result.allowed:
        raise Exception("rate_limit_exceeded")


async def _get_api_wallet(db: AsyncSession, customer: ApiCustomer) -> ApiWallet:
    stmt = select(ApiWallet).where(ApiWallet.api_customer_id == customer.id)
    wallet = (await db.execute(stmt)).scalar_one_or_none()
    if wallet is None:
        # Create wallet on first use.
        wallet = ApiWallet(
            api_customer_id=customer.id,
            balance=Decimal("0"),
            currency="INR",
        )
        db.add(wallet)
        await db.flush()
    return wallet


async def _debit_api_wallet(
    db: AsyncSession,
    wallet: ApiWallet,
    amount: Decimal,
    reference_id: str,
    idempotency_key: str,
) -> ApiWalletTransaction:
    """
    Atomic debit from API wallet.
    Enforces non-negative balance; raises ApiBalanceError if insufficient.
    """
    if wallet.balance < amount:
        raise ApiBalanceError(
            detail=f"API wallet balance {wallet.balance} < required {amount}",
        )
    balance_before = wallet.balance
    wallet.balance = wallet.balance - amount

    tx = ApiWalletTransaction(
        id=str(uuid.uuid4()),
        api_wallet_id=wallet.id,
        tx_type="API_ORDER_DEBIT",
        amount=-amount,
        balance_before=balance_before,
        balance_after=wallet.balance,
        reference_id=reference_id,
        idempotency_key=idempotency_key,
    )
    db.add(tx)
    await db.flush()
    return tx


def _error(message: str) -> JSONResponse:
    return JSONResponse({"error": message})


def _ok(**kwargs) -> JSONResponse:
    return JSONResponse(kwargs)


# ── Main dispatcher ────────────────────────────────────────────────────────────

@router.post("")
@router.post("/")
async def api_dispatch(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """
    Single-endpoint dispatcher for the own SMM API.
    Reads `key` and `action` from form data (POST body).
    Routes to the appropriate handler based on `action`.
    """
    await assert_api_allowed()

    try:
        form = await request.form()
    except Exception:
        return _error("Invalid request body")

    raw_key: str = str(form.get("key", ""))
    action:  str = str(form.get("action", "")).lower().strip()

    if not raw_key:
        return _error("API key is required")
    if not action:
        return _error("Action is required")

    # Authenticate.
    try:
        customer = await _authenticate_api_key(db, raw_key)
    except ApiKeyError:
        return _error("Invalid API key")

    # Rate limit.
    key_hash = hash_api_key(raw_key)
    try:
        await _check_api_rate_limit(customer, key_hash)
    except Exception:
        return _error("Rate limit exceeded")

    # Dispatch.
    handlers = {
        "services":      _action_services,
        "add":           _action_add,
        "status":        _action_status,
        "multi_status":  _action_multi_status,
        "refill":        _action_refill,
        "refill_status": _action_refill_status,
        "multi_refill":  _action_multi_refill,
        "cancel":        _action_cancel,
        "balance":       _action_balance,
    }

    handler = handlers.get(action)
    if handler is None:
        return _error(f"Unknown action: {action!r}")

    try:
        return await handler(db=db, customer=customer, form=form)
    except ApiBalanceError:
        return _error("Insufficient balance")
    except ApiKeyError:
        return _error("Invalid API key")
    except Exception as exc:
        logger.error("api_action_error", action=action, error=str(exc))
        return _error("An error occurred. Please try again.")


# ── Action handlers ────────────────────────────────────────────────────────────

async def _action_services(db, customer, form) -> JSONResponse:
    """
    Return the platform's canonical service catalog.
    Provider IDs and provider service IDs are NEVER included.
    """
    services, total = await list_active_services(db, page=1, page_size=10000)

    result = []
    for svc in services:
        from app.services.catalog import resolve_provider_mapping
        from app.core.models import ProviderService as PSRow
        from app.pricing.engine import calculate_price

        try:
            mapping = await resolve_provider_mapping(db, svc.id)
            stmt = select(PSRow).where(
                PSRow.provider_id == mapping.provider_id,
                PSRow.provider_svc_id == mapping.provider_svc_id,
                PSRow.is_active == True,
            )
            ps = (await db.execute(stmt)).scalar_one_or_none()
            rate    = ps.rate    if ps and ps.rate    else Decimal("0")
            min_qty = ps.min_qty if ps and ps.min_qty else 1
            max_qty = ps.max_qty if ps and ps.max_qty else 1_000_000
            refill  = ps.refill  if ps else False
            cancel  = ps.cancel  if ps else False

            price_result = await calculate_price(
                db=db, service=svc, quantity=1000,
                provider_rate=rate,
                provider_currency="USD", target_currency="INR",
                usd_to_inr_rate=_USD_TO_INR,
            )
            rate_display = str(price_result.customer_price)
        except Exception:
            rate_display = "0"
            min_qty = 1; max_qty = 1_000_000
            refill  = False; cancel = False

        result.append({
            "service":     svc.public_id,   # canonical ID — no provider ID
            "name":        svc.display_name,
            "category":    svc.category.name if svc.category else "",
            "type":        "Default",
            "rate":        rate_display,
            "min":         str(min_qty),
            "max":         str(max_qty),
            "refill":      refill,
            "cancel":      cancel,
            "description": svc.description or "",
        })

    return JSONResponse(result)


async def _action_add(db, customer, form) -> JSONResponse:
    """
    Create a new order. Debits the API wallet.
    Accepts the platform's canonical service public_id as `service`.
    """
    service_id_raw = str(form.get("service", "")).strip()
    quantity_raw   = str(form.get("quantity", "")).strip()
    link           = str(form.get("link", "")).strip() or None
    idem_key       = str(form.get("idempotency_key", "")) or str(uuid.uuid4())

    if not service_id_raw:
        return _error("service is required")
    if not quantity_raw.isdigit():
        return _error("quantity must be a positive integer")

    quantity = int(quantity_raw)
    public_id = service_id_raw.upper()
    if not public_id.startswith("SVC-"):
        return _error("Invalid service ID format. Use platform service IDs (e.g. SVC-0001).")

    try:
        svc = await get_service_by_public_id(db, public_id)
    except ServiceNotFoundError:
        return _error("Service not found")

    await assert_orders_allowed(service_id=svc.id, category_id=svc.category_id)

    # Calculate price.
    from app.services.catalog import resolve_provider_mapping
    from app.core.models import ProviderService as PSRow
    from app.pricing.engine import calculate_price

    try:
        mapping = await resolve_provider_mapping(db, svc.id)
        stmt = select(PSRow).where(
            PSRow.provider_id == mapping.provider_id,
            PSRow.provider_svc_id == mapping.provider_svc_id,
        )
        ps      = (await db.execute(stmt)).scalar_one_or_none()
        rate    = ps.rate if ps and ps.rate else Decimal("0")

        price_result = await calculate_price(
            db=db, service=svc, quantity=quantity,
            provider_rate=rate,
            provider_currency="USD", target_currency="INR",
            usd_to_inr_rate=_USD_TO_INR,
        )
        price = price_result.customer_price
    except Exception as exc:
        return _error(f"Pricing error: {exc}")

    # Check and debit API wallet.
    api_wallet = await _get_api_wallet(db, customer)
    order_id   = str(uuid.uuid4())

    try:
        await _debit_api_wallet(
            db=db, wallet=api_wallet, amount=price,
            reference_id=order_id,
            idempotency_key=f"api_order:{idem_key}",
        )
    except ApiBalanceError:
        return _error("Insufficient balance")

    # Submit via order engine.
    try:
        order = await create_order(
            db=db, registry=registry,
            user_id=customer.tenant_id or 0,   # API customer identified by tenant
            service_id=svc.id,
            quantity=quantity,
            link=link,
            source="api",
            api_customer_id=customer.id,
            idempotency_key=idem_key,
            usd_to_inr_rate=_USD_TO_INR,
        )
    except DuplicateOrderError:
        # Refund the debit since we're not creating a new order.
        await _refund_api_wallet(db, api_wallet, price, order_id)
        # Find existing order and return it.
        stmt = select(Order).where(Order.idempotency_key == idem_key)
        existing = (await db.execute(stmt)).scalar_one_or_none()
        if existing:
            return _ok(order=existing.public_ref)
        return _error("Duplicate order")
    except Exception as exc:
        # Refund on any failure.
        await _refund_api_wallet(db, api_wallet, price, order_id)
        return _error(f"Order failed: {exc}")

    return _ok(order=order.public_ref)


async def _refund_api_wallet(
    db, wallet: ApiWallet, amount: Decimal, reference_id: str
) -> None:
    """Compensating credit to API wallet on order failure."""
    try:
        balance_before   = wallet.balance
        wallet.balance   = wallet.balance + amount
        refund_tx = ApiWalletTransaction(
            id=str(uuid.uuid4()),
            api_wallet_id=wallet.id,
            tx_type="API_REFUND",
            amount=amount,
            balance_before=balance_before,
            balance_after=wallet.balance,
            reference_id=reference_id,
            idempotency_key=f"api_refund:{reference_id}",
        )
        db.add(refund_tx)
        await db.flush()
    except Exception as exc:
        logger.error("api_wallet_refund_failed", error=str(exc))


async def _action_status(db, customer, form) -> JSONResponse:
    """Single order status by public_ref."""
    order_ref = str(form.get("order", "")).strip()
    if not order_ref:
        return _error("order is required")

    stmt = select(Order).where(
        Order.public_ref == order_ref,
        Order.api_customer_id == customer.id,
    )
    order = (await db.execute(stmt)).scalar_one_or_none()
    if order is None:
        return _error("Order not found")

    return _ok(
        charge=str(order.price_charged),
        start_count=order.start_count or 0,
        status=order.status.title(),
        remains=order.remains or 0,
        currency="INR",
    )


async def _action_multi_status(db, customer, form) -> JSONResponse:
    """Batch order status — comma-separated public_refs, max 100."""
    orders_raw = str(form.get("orders", "")).strip()
    if not orders_raw:
        return _error("orders is required")

    refs = [r.strip() for r in orders_raw.split(",") if r.strip()][:100]
    stmt = select(Order).where(
        Order.public_ref.in_(refs),
        Order.api_customer_id == customer.id,
    )
    orders = list((await db.execute(stmt)).scalars().all())
    order_map = {o.public_ref: o for o in orders}

    result = {}
    for ref in refs:
        o = order_map.get(ref)
        if o:
            result[ref] = {
                "charge":      str(o.price_charged),
                "start_count": o.start_count or 0,
                "status":      o.status.title(),
                "remains":     o.remains or 0,
                "currency":    "INR",
            }
        else:
            result[ref] = {"error": "Not found"}
    return JSONResponse(result)


async def _action_refill(db, customer, form) -> JSONResponse:
    """Request a refill for an order."""
    order_ref = str(form.get("order", "")).strip()
    stmt = select(Order).where(
        Order.public_ref == order_ref,
        Order.api_customer_id == customer.id,
        Order.refill_eligible == True,
    )
    order = (await db.execute(stmt)).scalar_one_or_none()
    if order is None:
        return _error("Order not found or not eligible for refill")

    try:
        result = await registry.refill(order.provider_id, order.provider_order_id)
        return _ok(refill=result.refill_id)
    except Exception as exc:
        return _error(f"Refill failed: {exc}")


async def _action_refill_status(db, customer, form) -> JSONResponse:
    """Single refill status."""
    refill_id = str(form.get("refill", "")).strip()
    if not refill_id:
        return _error("refill is required")

    # Find the order's provider_id to route the refill status check.
    # For now return a generic pending status — full wiring in Phase 17.
    return _ok(status="Pending")


async def _action_multi_refill(db, customer, form) -> JSONResponse:
    """Batch refill status — comma-separated refill IDs, max 100."""
    refills_raw = str(form.get("refills", "")).strip()
    if not refills_raw:
        return _error("refills is required")

    ids = [r.strip() for r in refills_raw.split(",") if r.strip()][:100]
    return JSONResponse({rid: {"status": "Pending"} for rid in ids})


async def _action_cancel(db, customer, form) -> JSONResponse:
    """Cancel an order."""
    order_ref = str(form.get("order", "")).strip()
    stmt = select(Order).where(
        Order.public_ref == order_ref,
        Order.api_customer_id == customer.id,
        Order.cancel_eligible == True,
    )
    order = (await db.execute(stmt)).scalar_one_or_none()
    if order is None:
        return _error("Order not found or cannot be cancelled")

    try:
        result = await registry.cancel(order.provider_id, order.provider_order_id)
        if result.success:
            from app.orders.engine import update_order_status
            await update_order_status(db, order.id, "cancelled")
            return JSONResponse([{"order": order_ref, "cancel": {"1": "success"}}])
        return _error("Cancellation rejected by provider")
    except Exception as exc:
        return _error(f"Cancel failed: {exc}")


async def _action_balance(db, customer, form) -> JSONResponse:
    """Return the API wallet balance."""
    api_wallet = await _get_api_wallet(db, customer)
    return _ok(balance=str(api_wallet.balance), currency="INR")
