"""
Mini App API routes.

Security: user_id ALWAYS from ctx.user_id (session) — never from body.
Provider fields NEVER in any response.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Query

from app.auth.dependencies import get_current_user
from app.core.database import AsyncSessionLocal
from app.core.logging import get_logger
from app.miniapp.schemas import (
    ApplyCouponRequest,
    CreateDepositRequest,
    CreateOrderRequest,
    PaginationParams,
    PricePreviewRequest,
)

logger = get_logger(__name__)
router = APIRouter(prefix="/miniapp/api", tags=["miniapp"])


# ── Auth ───────────────────────────────────────────────────────────────────────

@router.post("/auth")
async def miniapp_auth(
    x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
    body: dict = None,
):
    """
    Verify Telegram initData and return a session token.
    The session token is used in subsequent requests as Bearer token.
    """
    from app.auth.telegram import verify_init_data
    from app.auth.sessions import create_session
    from app.core.exceptions import InvalidInitDataError

    init_data = x_telegram_init_data or (body or {}).get("init_data", "")
    if not init_data:
        raise HTTPException(status_code=400, detail="initData required")

    try:
        user_data = verify_init_data(init_data)
    except InvalidInitDataError as e:
        raise HTTPException(status_code=401, detail=e.user_message)

    telegram_id = user_data["id"]
    session_token = await create_session(
        user_id=telegram_id,
        session_type="miniapp",
        extra={"telegram_id": telegram_id, "first_name": user_data.get("first_name", "")},
    )
    return {"session_token": session_token, "user": user_data}


# ── Categories ────────────────────────────────────────────────────────────────

@router.get("/categories")
async def list_categories(ctx=Depends(get_current_user)):
    """List all active service categories."""
    from sqlalchemy import select
    from app.core.models import Category

    async with AsyncSessionLocal() as db:
        cats = list((await db.execute(
            select(Category).where(Category.is_active == True)
            .order_by(Category.sort_order, Category.id)
        )).scalars().all())

    return {
        "items": [
            {"id": c.id, "name": c.name, "slug": c.slug}
            for c in cats
        ]
    }


# ── Services ──────────────────────────────────────────────────────────────────

@router.get("/services")
async def list_services(
    category_id: int | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    ctx=Depends(get_current_user),
):
    """List active services. Response never includes provider fields."""
    from app.services.catalog import list_active_services
    from app.pricing.engine import calculate_price

    async with AsyncSessionLocal() as db:
        services, total = await list_active_services(
            db=db, category_id=category_id, page=page, page_size=page_size
        )

    items = []
    for svc in services:
        items.append({
            "public_id":       svc.public_id,
            "display_name":    svc.display_name,
            "min_qty":         0,
            "max_qty":         0,
            "price_per_1000":  str(svc.custom_price or "0"),
            "refill_eligible": svc.refill_enabled,
            "cancel_eligible": svc.cancel_enabled,
        })

    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.get("/services/{public_id}")
async def get_service(public_id: str, ctx=Depends(get_current_user)):
    """Get service detail. No provider fields in response."""
    from app.services.catalog import get_service_by_public_id
    from app.core.exceptions import ServiceNotFoundError

    async with AsyncSessionLocal() as db:
        try:
            svc = await get_service_by_public_id(db, public_id)
        except ServiceNotFoundError as e:
            raise HTTPException(status_code=404, detail=e.user_message)

    return {
        "public_id":       svc.public_id,
        "display_name":    svc.display_name,
        "min_qty":         0,
        "max_qty":         0,
        "price_per_1000":  str(svc.custom_price or "0"),
        "refill_eligible": svc.refill_enabled,
        "cancel_eligible": svc.cancel_enabled,
    }


# ── Price preview ─────────────────────────────────────────────────────────────

@router.post("/orders/preview")
async def price_preview(
    req: PricePreviewRequest,
    ctx=Depends(get_current_user),
):
    """Preview order price. No provider fields. user_id from session."""
    user_id = ctx["user_id"]  # ALWAYS from session

    async with AsyncSessionLocal() as db:
        from app.services.catalog import get_service_by_public_id
        try:
            svc = await get_service_by_public_id(db, req.service_public_id)
        except Exception as e:
            raise HTTPException(status_code=404, detail=str(e))

    return {
        "service_public_id": svc.public_id,
        "quantity":          req.quantity,
        "price":             str(svc.custom_price or "0"),
        "currency":          "INR",
        "coupon_applied":    False,
    }


# ── Orders ────────────────────────────────────────────────────────────────────

@router.post("/orders")
async def create_order(
    req: CreateOrderRequest,
    ctx=Depends(get_current_user),
):
    """
    Create an order. user_id ALWAYS from ctx.user_id — never from body.
    Price recalculated server-side.
    """
    user_id = ctx["user_id"]   # SECURITY: from session, not body

    from app.orders.engine import create_order as engine_create
    from app.core.exceptions import OrderError, InsufficientBalanceError
    from app.services.catalog import get_service_by_public_id

    async with AsyncSessionLocal() as db:
        try:
            svc = await get_service_by_public_id(db, req.service_public_id)
        except Exception as e:
            raise HTTPException(status_code=404, detail=str(e))

        try:
            order = await engine_create(
                db=db,
                user_id=user_id,
                service_id=svc.id,
                quantity=req.quantity,
                link=req.link,
                idempotency_key=req.idempotency_key,
                source="miniapp",
                coupon_code=req.coupon_code,
            )
            await db.commit()
        except InsufficientBalanceError as e:
            raise HTTPException(status_code=422, detail=e.user_message)
        except OrderError as e:
            raise HTTPException(status_code=422, detail=e.user_message)

    return {
        "id":                order.id,
        "public_ref":        order.public_ref,
        "service_public_id": req.service_public_id,
        "service_name":      svc.display_name,
        "status":            order.status,
        "quantity":          order.quantity,
        "price_charged":     str(order.price_charged),
        "currency":          order.currency,
        "refill_eligible":   order.refill_eligible,
        "cancel_eligible":   order.cancel_eligible,
        "source":            order.source,
        "created_at":        order.created_at.isoformat(),
    }


@router.get("/orders")
async def list_orders(
    page: int = Query(default=1, ge=1),
    ctx=Depends(get_current_user),
):
    """List orders for the authenticated user."""
    user_id = ctx["user_id"]
    from sqlalchemy import select
    from app.core.models import Order

    async with AsyncSessionLocal() as db:
        orders = list((await db.execute(
            select(Order)
            .where(Order.user_id == user_id)
            .order_by(Order.created_at.desc())
            .limit(20).offset((page - 1) * 20)
        )).scalars().all())

    return {
        "items": [
            {
                "id": o.id, "public_ref": o.public_ref,
                "service_name": "",   # full impl loads service name
                "status": o.status,   "quantity": o.quantity,
                "price_charged": str(o.price_charged),
                "created_at": o.created_at.isoformat(),
            }
            for o in orders
        ],
        "page": page,
    }


@router.get("/orders/{order_id}")
async def get_order(order_id: str, ctx=Depends(get_current_user)):
    """Get order detail for the authenticated user (IDOR: user_id checked)."""
    user_id = ctx["user_id"]
    from app.core.models import Order

    async with AsyncSessionLocal() as db:
        order = await db.get(Order, order_id)

    if order is None or order.user_id != user_id:
        raise HTTPException(status_code=404, detail="Order not found")

    return {
        "id": order.id, "public_ref": order.public_ref,
        "status": order.status, "quantity": order.quantity,
        "price_charged": str(order.price_charged),
        "currency": order.currency,
        "refill_eligible": order.refill_eligible,
        "cancel_eligible": order.cancel_eligible,
        "remains": order.remains, "start_count": order.start_count,
        "link": order.link, "source": order.source,
        "created_at": order.created_at.isoformat(),
    }


# ── Wallet ────────────────────────────────────────────────────────────────────

@router.get("/wallet")
async def get_wallet(ctx=Depends(get_current_user)):
    """Get wallet balance for the authenticated user."""
    user_id = ctx["user_id"]
    from sqlalchemy import select
    from app.core.models import Wallet

    async with AsyncSessionLocal() as db:
        wallet = (await db.execute(
            select(Wallet).where(Wallet.user_id == user_id)
        )).scalar_one_or_none()

    if wallet is None:
        return {"balance": "0.00", "currency": "INR"}
    return {"balance": str(wallet.balance.quantize(__import__('decimal').Decimal("0.01"))), "currency": wallet.currency}


@router.post("/wallet/deposit")
async def create_deposit(
    req: CreateDepositRequest,
    ctx=Depends(get_current_user),
):
    """Create a payment intent. user_id from session."""
    user_id = ctx["user_id"]
    import uuid as _uuid
    from app.core.models import Payment
    from datetime import datetime, timezone

    provider_ref = f"SMM-{_uuid.uuid4().hex[:8].upper()}"

    async with AsyncSessionLocal() as db:
        payment = Payment(
            user_id=user_id,
            amount=req.amount,
            currency="INR",
            provider=req.provider,
            provider_ref=provider_ref,
            status="pending",
            webhook_verified=False,
            created_at=datetime.now(timezone.utc),
        )
        db.add(payment)
        await db.commit()
        await db.refresh(payment)

    return {
        "payment_id":   payment.id,
        "provider_ref": provider_ref,
        "amount":       str(req.amount),
        "provider":     req.provider,
        "payment_link": None,
    }


# ── Profile ───────────────────────────────────────────────────────────────────

@router.get("/profile")
async def get_profile(ctx=Depends(get_current_user)):
    """Get profile for the authenticated user."""
    user_id = ctx["user_id"]
    from sqlalchemy import select, func
    from app.core.models import User, Order

    async with AsyncSessionLocal() as db:
        user = (await db.execute(
            select(User).where(User.id == user_id)
        )).scalar_one_or_none()

        order_count = (await db.execute(
            select(func.count()).where(Order.user_id == user_id)
        )).scalar_one()

    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    return {
        "telegram_id":  user.telegram_id,
        "first_name":   user.first_name or "",
        "username":     user.username,
        "is_premium":   user.is_premium,
        "plan":         "premium" if user.is_premium else "basic",
        "total_orders": order_count,
        "member_since": user.created_at.isoformat() if user.created_at else None,
    }
