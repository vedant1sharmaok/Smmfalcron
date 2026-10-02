"""Mini App JSON API. Client-supplied prices are ignored; quotes are always server-side."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.api.limits import check_order
from app.api.telegram_auth import DbDep, UserDep
from app.catalog import (
    featured_stars,
    get_setting,
    get_service,
    kill_switches,
    list_categories,
)
from app.config import get_settings
from app.models import Payment, Service
from app.payments.service import DepositError, check_payment, create_deposit, settle_payment
from app.orders import (
    OrderError,
    get_order_for_user,
    list_orders,
    place_order,
    request_cancel,
    request_refill,
    status_label,
    sync_order_status,
)
from app.pricing import paise_to_rupees_str, quote_order
from app.wallet import InsufficientFunds, list_entries, snapshot_of

router = APIRouter(prefix="/api", tags=["miniapp"])


class QuoteIn(BaseModel):
    service_id: str = Field(min_length=1, max_length=64)
    quantity: int = Field(gt=0, le=1_000_000_000)
    coupon_code: str | None = Field(default=None, max_length=32)
    # Intentionally ignored if a client sends a price:
    client_price_paise: int | None = None


class OrderIn(BaseModel):
    service_id: str = Field(min_length=1, max_length=64)
    link: str = Field(min_length=1, max_length=512)
    quantity: int = Field(gt=0, le=1_000_000_000)
    comments: str | None = Field(default=None, max_length=20_000)
    mentions: str | None = Field(default=None, max_length=20_000)
    coupon_code: str | None = Field(default=None, max_length=32)
    idempotency_key: str | None = Field(default=None, max_length=128)
    client_price_paise: int | None = None


class DepositIn(BaseModel):
    amount_paise: int = Field(gt=99, le=100_000_000)
    idempotency_key: str | None = Field(default=None, max_length=128)


def _order_out(order) -> dict[str, Any]:
    return {
        "public_id": order.public_id,
        "status": order.status,
        "status_label": status_label(order.status),
        "service_id": order.service_id,
        "link": order.link,
        "quantity": order.quantity,
        "charge_paise": order.charge_paise,
        "charge_display": paise_to_rupees_str(order.charge_paise),
        "start_count": order.start_count,
        "remains": order.remains,
        "coupon_code": order.coupon_code,
        "fail_reason": order.fail_reason,
        "created_at": order.created_at.isoformat() if order.created_at else None,
    }


@router.get("/me")
async def me(user: UserDep) -> dict[str, Any]:
    snap = snapshot_of(user)
    return {
        "telegram_id": user.telegram_id,
        "username": user.username,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "language_code": user.language_code,
        "name": user.display_name,
        "role": user.role,
        "terms_accepted": user.terms_accepted_at is not None,
        "referral_code": user.referral_code,
        "wallet": snap.as_dict(),
        "wallet_display": {
            "available": paise_to_rupees_str(snap.available_paise),
            "reserved": paise_to_rupees_str(snap.reserved_paise),
            "balance": paise_to_rupees_str(snap.balance_paise),
        },
    }


def _service_out(svc: Service, sell: int) -> dict[str, Any]:
    return {
        "id": svc.id,
        "name": svc.name,
        "description": svc.description,
        "type": svc.service_type,
        "min_qty": svc.min_qty,
        "max_qty": svc.max_qty,
        "sell_per_1000_paise": sell,
        "sell_per_1000_display": paise_to_rupees_str(sell),
        "refillable": svc.is_refillable,
        "cancelable": svc.is_cancelable,
        "average_time": svc.average_time,
        "category_id": svc.category_id,
    }


@router.get("/config")
async def app_config(session: DbDep, user: UserDep) -> dict[str, Any]:
    settings = get_settings()
    flags = await kill_switches(session)
    return {
        "brand": await get_setting(session, "brand_name", "FALARON"),
        "currency": settings.currency_code,
        "symbol": settings.currency_symbol,
        "min_deposit_paise": settings.min_deposit_paise,
        "max_deposit_paise": settings.max_deposit_paise,
        "payment_method": settings.payment_gateway,
        "demo_payments": settings.mock_payments_allowed,
        "terms_url": await get_setting(session, "terms_url", ""),
        "privacy_url": await get_setting(session, "privacy_url", ""),
        "flags": flags,
    }


@router.get("/catalog")
async def catalog(session: DbDep, user: UserDep) -> dict[str, Any]:
    """Categories with live service counts. Services load per category (paged) so a
    synced catalog of thousands of services never turns into thousands of queries."""
    cats = await list_categories(session)
    counts = dict(
        (
            await session.execute(
                select(Service.category_id, func.count())
                .where(Service.is_active.is_(True))
                .group_by(Service.category_id)
            )
        ).all()
    )
    payload = [
        {
            "id": cat.id,
            "name": cat.name,
            "emoji": cat.emoji,
            "description": cat.description,
            "service_count": int(counts.get(cat.id, 0)),
        }
        for cat in cats
        if counts.get(cat.id, 0)
    ]
    stars = []
    for star, svc in await featured_stars(session):
        stars.append(
            {
                "id": star.id,
                "label": star.label,
                "service_id": svc.id,
                "name": svc.name,
                "rate_per_1000_paise": star.custom_rate_per_1000_paise,
                "rate_display": paise_to_rupees_str(star.custom_rate_per_1000_paise),
            }
        )
    return {"categories": payload, "stars": stars}


@router.get("/categories/{category_id}/services")
async def category_services(
    category_id: str, session: DbDep, user: UserDep, limit: int = 40, offset: int = 0
) -> dict[str, Any]:
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    rows = (
        (
            await session.execute(
                select(Service)
                .where(Service.category_id == category_id, Service.is_active.is_(True))
                .order_by(Service.name)
                .offset(offset)
                .limit(limit + 1)
            )
        )
        .scalars()
        .all()
    )
    has_more = len(rows) > limit
    items = []
    for svc in rows[:limit]:
        try:
            q = await quote_order(session, service_id=svc.id, quantity=svc.min_qty, user_id=user.telegram_id)
        except ValueError:
            continue  # never fall back to the provider cost: that would leak our margin
        items.append(_service_out(svc, q.sell_per_1000_paise))
    return {"services": items, "has_more": has_more}


@router.post("/quote")
async def quote(body: QuoteIn, session: DbDep, user: UserDep) -> dict[str, Any]:
    # client_price_paise is discarded on purpose
    try:
        q = await quote_order(
            session,
            service_id=body.service_id,
            quantity=body.quantity,
            user_id=user.telegram_id,
            coupon_code=body.coupon_code,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    out = q.as_dict()
    # Provider cost and reseller internals are business data: never sent to customers.
    for hidden in ("cost_per_1000_paise", "cost_paise", "layer", "reseller_discount_paise"):
        out.pop(hidden, None)
    return out


@router.post("/orders")
async def create_order(body: OrderIn, session: DbDep, user: UserDep) -> dict[str, Any]:
    check_order(user.telegram_id)
    extra: dict[str, Any] = {}
    if body.comments:
        extra["comments"] = body.comments
    if body.mentions:
        extra["mentions"] = body.mentions
    try:
        order = await place_order(
            session,
            user,
            service_id=body.service_id,
            link=body.link,
            quantity=body.quantity,
            extra=extra,
            coupon_code=body.coupon_code,
            idempotency_key=body.idempotency_key,
        )
    except InsufficientFunds as exc:
        raise HTTPException(status_code=402, detail=str(exc)) from exc
    except (OrderError, PermissionError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _order_out(order)


@router.get("/orders")
async def orders(session: DbDep, user: UserDep, limit: int = 20, offset: int = 0) -> dict[str, Any]:
    rows = await list_orders(session, user.telegram_id, limit=min(limit, 50), offset=offset)
    return {"orders": [_order_out(o) for o in rows]}


@router.get("/orders/{public_id}")
async def order_detail(public_id: str, session: DbDep, user: UserDep) -> dict[str, Any]:
    order = await get_order_for_user(session, public_id, user.telegram_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    return _order_out(order)


@router.post("/orders/{public_id}/refresh")
async def order_refresh(public_id: str, session: DbDep, user: UserDep) -> dict[str, Any]:
    order = await get_order_for_user(session, public_id, user.telegram_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    order = await sync_order_status(session, order)
    return _order_out(order)


@router.post("/orders/{public_id}/refill")
async def order_refill(public_id: str, session: DbDep, user: UserDep) -> dict[str, Any]:
    order = await get_order_for_user(session, public_id, user.telegram_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    try:
        order = await request_refill(session, order, user)
    except (OrderError, PermissionError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _order_out(order)


@router.post("/orders/{public_id}/cancel")
async def order_cancel(public_id: str, session: DbDep, user: UserDep) -> dict[str, Any]:
    order = await get_order_for_user(session, public_id, user.telegram_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    try:
        order = await request_cancel(session, order, user)
    except (OrderError, PermissionError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _order_out(order)


@router.get("/wallet")
async def wallet(session: DbDep, user: UserDep) -> dict[str, Any]:
    snap = snapshot_of(user)
    entries = await list_entries(session, user.telegram_id, limit=25)
    return {
        "wallet": snap.as_dict(),
        "wallet_display": {
            "available": paise_to_rupees_str(snap.available_paise),
            "reserved": paise_to_rupees_str(snap.reserved_paise),
            "balance": paise_to_rupees_str(snap.balance_paise),
        },
        "ledger": [
            {
                "id": e.id,
                "type": e.entry_type,
                "amount_paise": e.amount_paise,
                "amount_display": paise_to_rupees_str(e.amount_paise),
                "reason": e.reason,
                "balance_after_paise": e.balance_after_paise,
                "created_at": e.created_at.isoformat() if e.created_at else None,
            }
            for e in entries
        ],
    }


def _payment_out(payment: Payment) -> dict[str, Any]:
    return {
        "public_id": payment.public_id,
        "status": payment.status,
        "amount_paise": payment.amount_paise,
        "amount_display": paise_to_rupees_str(payment.amount_paise),
        "method": payment.method,
        "checkout_url": payment.checkout_url if payment.status == "pending" else None,
        "failure_reason": None,  # internal detail; the user only needs the status
    }


@router.post("/deposit")
async def deposit(body: DepositIn, session: DbDep, user: UserDep) -> dict[str, Any]:
    settings = get_settings()
    try:
        payment = await create_deposit(
            session, user, body.amount_paise, settings=settings, idempotency_key=body.idempotency_key
        )
    except DepositError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    out = _payment_out(payment)
    if payment.method == "manual":
        out["instructions"] = await get_setting(session, "payment_instructions", "")
    return out


@router.get("/deposit/{public_id}")
async def deposit_status(public_id: str, session: DbDep, user: UserDep) -> dict[str, Any]:
    """Polled by the Mini App after the customer returns from the payment page."""
    payment = await check_payment(session, public_id, user.telegram_id, settings=get_settings())
    if payment is None:
        raise HTTPException(status_code=404, detail="Invoice not found")
    await session.refresh(user)
    out = _payment_out(payment)
    out["wallet"] = snapshot_of(user).as_dict()
    return out


@router.post("/deposit/{public_id}/mock-pay")
async def mock_pay(public_id: str, session: DbDep, user: UserDep) -> dict[str, Any]:
    """Development only. In production this route does not exist as far as clients can tell."""
    settings = get_settings()
    if not settings.mock_payments_allowed:
        raise HTTPException(status_code=404, detail="Not found")
    payment = (
        await session.execute(select(Payment).where(Payment.public_id == public_id))
    ).scalar_one_or_none()
    if payment is None or payment.user_id != user.telegram_id or payment.method != "mock":
        raise HTTPException(status_code=404, detail="Invoice not found")
    await settle_payment(
        session,
        payment_public_id=payment.public_id,
        gateway="mock",
        gateway_ref=None,
        amount_paise=payment.amount_paise,
        currency=payment.currency,
        source="mock",
        actor=str(user.telegram_id),
    )
    await session.refresh(user)
    return {"public_id": payment.public_id, "status": payment.status, "wallet": snapshot_of(user).as_dict()}


@router.get("/services/{service_id}")
async def service_detail(service_id: str, session: DbDep, user: UserDep) -> dict[str, Any]:
    svc = await get_service(session, service_id)
    if svc is None or not svc.is_active:
        raise HTTPException(status_code=404, detail="Service not found")
    try:
        q = await quote_order(session, service_id=svc.id, quantity=svc.min_qty, user_id=user.telegram_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Service not available") from exc
    return _service_out(svc, q.sell_per_1000_paise)
