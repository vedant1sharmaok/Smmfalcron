"""
Orders handler — list, detail, refresh, cancel, refills.
Sections 13, 14, 15, 16 of blueprint.
Backend determines all eligibility — frontend never decides.
"""
from __future__ import annotations
from decimal import Decimal
from aiogram import F, Router
from aiogram.types import CallbackQuery
from app.bot.keyboards import CB, back_home_keyboard, orders_keyboard, order_detail_keyboard, refills_keyboard, refill_confirm_keyboard
from app.bot.messages import orders_header, order_detail_card, refills_header, refill_confirm, refill_submitted, error_card
from app.bot.safe_send import safe_edit
from app.core.logging import get_logger

logger = get_logger(__name__)
router = Router(name="orders")
PAGE_SIZE = 8


async def _get_user_id(db, telegram_id: int) -> int | None:
    from sqlalchemy import select
    from app.core.models import User
    result = await db.execute(select(User).where(User.telegram_id == telegram_id))
    user   = result.scalar_one_or_none()
    return user.id if user else None


@router.callback_query(F.data.startswith("ord:list:"))
async def show_orders(call: CallbackQuery) -> None:
    await call.answer()
    page   = int(call.data.split(":")[2]) if len(call.data.split(":")) > 2 else 1

    from app.core.database import AsyncSessionLocal
    from sqlalchemy import select, func
    from app.core.models import Order, Service

    async with AsyncSessionLocal() as db:
        user_id = await _get_user_id(db, call.from_user.id)
        if not user_id:
            await safe_edit(call.message, error_card("Session expired."), reply_markup=back_home_keyboard()); return

        total = (await db.execute(
            select(func.count()).where(Order.user_id == user_id)
        )).scalar_one()

        rows = list((await db.execute(
            select(Order)
            .where(Order.user_id == user_id)
            .order_by(Order.created_at.desc())
            .offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE)
        )).scalars().all())

        orders = []
        for o in rows:
            svc = await db.get(Service, o.service_id)
            orders.append({
                "id": o.id, "public_ref": o.public_ref,
                "service_name": svc.display_name if svc else "Service",
                "status": o.status,
            })

    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    await safe_edit(
        call.message,
        orders_header(total, page, total_pages),
        reply_markup=orders_keyboard(orders, page, total_pages),
    )


@router.callback_query(F.data.startswith("ord:detail:"))
async def show_order_detail(call: CallbackQuery) -> None:
    await call.answer()
    short_id = call.data.split(":")[2]

    from app.core.database import AsyncSessionLocal
    from sqlalchemy import select
    from app.core.models import Order, Service

    async with AsyncSessionLocal() as db:
        user_id = await _get_user_id(db, call.from_user.id)
        if not user_id:
            await safe_edit(call.message, error_card("Session expired."), reply_markup=back_home_keyboard()); return

        orders = list((await db.execute(
            select(Order).where(Order.user_id == user_id)
            .order_by(Order.created_at.desc()).limit(100)
        )).scalars().all())
        order = next((o for o in orders if o.id.startswith(short_id)), None)

        if not order or order.user_id != user_id:
            await safe_edit(call.message, error_card("Order not found."), reply_markup=back_home_keyboard()); return

        svc = await db.get(Service, order.service_id)

    created_str = order.created_at.strftime("%Y-%m-%d %H:%M") if order.created_at else "—"
    text = order_detail_card(
        public_ref=order.public_ref, service_name=svc.display_name if svc else "Service",
        status=order.status, quantity=order.quantity, price=order.price_charged,
        link=order.link, remains=order.remains, start_count=order.start_count,
        created_at=created_str, refill_eligible=order.refill_eligible,
        cancel_eligible=order.cancel_eligible,
    )
    await safe_edit(call.message, text,
        reply_markup=order_detail_keyboard(order.id, order.refill_eligible, order.cancel_eligible))


@router.callback_query(F.data.startswith("ord:refresh:"))
async def refresh_order(call: CallbackQuery) -> None:
    # Force status re-check — delegate to order monitor logic then show detail
    await call.answer("🔄 Refreshing status...", show_alert=False)
    new_data = "ord:detail:" + call.data.split(":")[2]
    call.data = new_data
    await show_order_detail(call)


@router.callback_query(F.data.startswith("ord:cancel:"))
async def cancel_order(call: CallbackQuery) -> None:
    await call.answer()
    short_id = call.data.split(":")[2]

    from app.core.database import AsyncSessionLocal
    from sqlalchemy import select
    from app.core.models import Order

    async with AsyncSessionLocal() as db:
        user_id = await _get_user_id(db, call.from_user.id)
        if not user_id:
            await safe_edit(call.message, error_card("Session expired."), reply_markup=back_home_keyboard()); return

        orders = list((await db.execute(
            select(Order).where(Order.user_id == user_id).limit(100)
        )).scalars().all())
        order = next((o for o in orders if o.id.startswith(short_id)), None)

        if not order or order.user_id != user_id:
            await safe_edit(call.message, error_card("Order not found."), reply_markup=back_home_keyboard()); return

        if not order.cancel_eligible:
            await call.answer("❌ This order cannot be cancelled.", show_alert=True); return

        try:
            from app.providers.registry import registry
            from app.providers.models import CancelRequest
            await registry.cancel_order(provider_id=order.provider_id, provider_order_id=order.provider_order_id)
        except Exception as exc:
            logger.warning("cancel_failed", order_id=order.id, error=str(exc))

        from app.orders.engine import update_order_status
        from app.orders.engine import refund_order
        await update_order_status(db, order.id, "cancelled")
        await refund_order(db, order, actor_id=user_id, reason="Customer cancellation")
        await db.commit()

    await safe_edit(
        call.message,
        "⚫ <b>Order Cancelled</b>\n\nYour order has been cancelled and your balance refunded.",
        reply_markup=back_home_keyboard(),
    )


# ── Refills ───────────────────────────────────────────────────────────────────

@router.callback_query(F.data == CB.REFILL_LIST)
async def show_refills(call: CallbackQuery) -> None:
    await call.answer()

    from app.core.database import AsyncSessionLocal
    from sqlalchemy import select
    from app.core.models import Order, Service

    async with AsyncSessionLocal() as db:
        user_id = await _get_user_id(db, call.from_user.id)
        if not user_id:
            await safe_edit(call.message, error_card("Session expired."), reply_markup=back_home_keyboard()); return

        eligible = list((await db.execute(
            select(Order).where(
                Order.user_id == user_id,
                Order.refill_eligible == True,
                Order.status.in_(["completed", "partial"]),
            ).order_by(Order.created_at.desc()).limit(20)
        )).scalars().all())

        orders = []
        for o in eligible:
            svc = await db.get(Service, o.service_id)
            orders.append({"id": o.id, "public_ref": o.public_ref,
                           "service_name": svc.display_name if svc else "Service"})

    await safe_edit(
        call.message,
        refills_header(len(orders)),
        reply_markup=refills_keyboard(orders) if orders else back_home_keyboard(),
    )


@router.callback_query(F.data.startswith("ref:do:"))
async def refill_do(call: CallbackQuery) -> None:
    await call.answer()
    short_id = call.data.split(":")[2]

    from app.core.database import AsyncSessionLocal
    from sqlalchemy import select
    from app.core.models import Order, Service

    async with AsyncSessionLocal() as db:
        user_id = await _get_user_id(db, call.from_user.id)
        orders  = list((await db.execute(select(Order).where(Order.user_id == user_id).limit(100))).scalars().all())
        order   = next((o for o in orders if o.id.startswith(short_id)), None)
        if not order or not order.refill_eligible:
            await call.answer("♻️ This order is not eligible for refill.", show_alert=True); return
        svc = await db.get(Service, order.service_id)

    await safe_edit(
        call.message,
        refill_confirm(order.public_ref, svc.display_name if svc else "Service"),
        reply_markup=refill_confirm_keyboard(short_id),
    )


@router.callback_query(F.data.startswith("ref:confirm:"))
async def refill_confirm_action(call: CallbackQuery) -> None:
    await call.answer("🔁 Submitting refill...")
    short_id = call.data.split(":")[2]

    from app.core.database import AsyncSessionLocal
    from sqlalchemy import select
    from app.core.models import Order

    async with AsyncSessionLocal() as db:
        user_id = await _get_user_id(db, call.from_user.id)
        orders  = list((await db.execute(select(Order).where(Order.user_id == user_id).limit(100))).scalars().all())
        order   = next((o for o in orders if o.id.startswith(short_id)), None)
        if not order or order.user_id != user_id:
            await safe_edit(call.message, error_card("Order not found."), reply_markup=back_home_keyboard()); return
        if not order.refill_eligible or not order.provider_order_id:
            await call.answer("Not eligible for refill.", show_alert=True); return

        try:
            from app.providers.registry import registry
            await registry.refill(order.provider_id, order.provider_order_id)
        except Exception as exc:
            logger.warning("refill_failed", order_id=order.id, error=str(exc))
            await safe_edit(call.message, error_card("Refill request failed. Try again later."), reply_markup=back_home_keyboard()); return

    await safe_edit(call.message, refill_submitted(order.public_ref), reply_markup=back_home_keyboard())
