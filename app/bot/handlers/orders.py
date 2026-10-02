"""Order list, details, refresh, refill, cancel."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.ads import apply_ad
from app.bot.handlers.start import ensure_user
from app.bot.menus import (
    MenuCB,
    OrdCB,
    back_home_kb,
    cancel_confirm_kb,
    html_escape,
    order_detail_kb,
    orders_kb,
    safe_edit,
)
from app.config import Settings
from app.models import Order
from app.orders import (
    OrderError,
    get_order_for_user,
    list_orders,
    request_cancel,
    request_refill,
    status_label,
    sync_order_status,
)
from app.pricing import paise_to_rupees_str

router = Router(name="orders")

PAGE_SIZE = 8


def _order_summary(order: Order) -> str:
    snap_name = ""
    try:
        import json

        snap = json.loads(order.service_snapshot_json or "{}")
        snap_name = str(snap.get("name") or "")
    except Exception:  # noqa: BLE001
        snap_name = ""
    name = html_escape(snap_name or (order.service.name if order.service else order.service_id))
    remains = order.remains if order.remains is not None else "—"
    start = order.start_count if order.start_count is not None else "—"
    extra = order.extra()
    extra_lines = ""
    if extra.get("comments"):
        extra_lines += "\nComments: provided"
    if extra.get("mentions"):
        extra_lines += f"\nMentions: {html_escape(str(extra.get('mentions'))[:120])}"
    fail = f"\nNote: {html_escape(order.fail_reason)}" if order.fail_reason else ""
    return (
        f"<b>{html_escape(order.public_id)}</b>\n"
        f"Service: {name}\n"
        f"Status: <b>{status_label(order.status)}</b>\n"
        f"Target: {html_escape(order.link)}\n"
        f"Quantity: {order.quantity:,} · Start: {start} · Remains: {remains}\n"
        f"Charged: {paise_to_rupees_str(order.charge_paise)}"
        f"{extra_lines}"
        f"{fail}"
    )


async def _render_list(
    target: CallbackQuery | Message, session: AsyncSession, user_id: int, page: int
) -> None:
    offset = page * PAGE_SIZE
    rows = await list_orders(session, user_id, limit=PAGE_SIZE + 1, offset=offset)
    has_next = len(rows) > PAGE_SIZE
    rows = rows[:PAGE_SIZE]
    if not rows and page == 0:
        text, markup = await apply_ad(
            session, "orders", "No orders yet. Open Services to place one.", back_home_kb()
        )
        await safe_edit(target, text, markup)
        return
    if not rows:
        await safe_edit(target, "No more orders on this page.", back_home_kb())
        return
    text, markup = await apply_ad(
        session, "orders", "<b>Your orders</b>\nTap a row for details.", orders_kb(rows, page, has_next)
    )
    await safe_edit(target, text, markup)


@router.callback_query(MenuCB.filter(F.a == "orders"))
async def cb_orders(query: CallbackQuery, session: AsyncSession) -> None:
    if query.from_user is None:
        return
    await _render_list(query, session, query.from_user.id, 0)


@router.callback_query(MenuCB.filter(F.a == "refills"))
async def cb_refills(query: CallbackQuery, session: AsyncSession) -> None:
    if query.from_user is None:
        return
    rows = await list_orders(session, query.from_user.id, limit=20, status="completed")
    partial = await list_orders(session, query.from_user.id, limit=20, status="partial")
    combined = rows + partial
    if not combined:
        await safe_edit(
            query,
            "<b>Refills</b>\nCompleted and partial orders appear here when refill is available.",
            back_home_kb(),
        )
        return
    await safe_edit(
        query,
        "<b>Refillable orders</b>\nOpen an order to request a refill.",
        orders_kb(combined[: PAGE_SIZE], 0, False),
    )


@router.callback_query(OrdCB.filter(F.a == "p"))
async def cb_orders_page(query: CallbackQuery, callback_data: OrdCB, session: AsyncSession) -> None:
    if query.from_user is None:
        return
    try:
        page = int(callback_data.i)
    except ValueError:
        page = 0
    await _render_list(query, session, query.from_user.id, max(page, 0))


@router.callback_query(OrdCB.filter(F.a == "v"))
async def cb_order_view(query: CallbackQuery, callback_data: OrdCB, session: AsyncSession) -> None:
    if query.from_user is None:
        return
    order = await get_order_for_user(session, callback_data.i, query.from_user.id)
    if order is None:
        await query.answer("Order not found", show_alert=True)
        return
    await safe_edit(query, _order_summary(order), order_detail_kb(order))


@router.callback_query(OrdCB.filter(F.a == "r"))
async def cb_order_refresh(query: CallbackQuery, callback_data: OrdCB, session: AsyncSession) -> None:
    if query.from_user is None:
        return
    order = await get_order_for_user(session, callback_data.i, query.from_user.id)
    if order is None:
        await query.answer("Order not found", show_alert=True)
        return
    order = await sync_order_status(session, order)
    await query.answer("Status updated")
    await safe_edit(query, _order_summary(order), order_detail_kb(order))


@router.callback_query(OrdCB.filter(F.a == "f"))
async def cb_order_refill(
    query: CallbackQuery, callback_data: OrdCB, session: AsyncSession, settings: Settings
) -> None:
    if query.from_user is None:
        return
    order = await get_order_for_user(session, callback_data.i, query.from_user.id)
    if order is None:
        await query.answer("Order not found", show_alert=True)
        return
    user = await ensure_user(session, query.from_user, settings)
    try:
        order = await request_refill(session, order, user)
    except (OrderError, PermissionError) as exc:
        await query.answer(str(exc), show_alert=True)
        return
    await safe_edit(query, "Refill requested.\n\n" + _order_summary(order), order_detail_kb(order))


@router.callback_query(OrdCB.filter(F.a == "x"))
async def cb_order_cancel_ask(query: CallbackQuery, callback_data: OrdCB, session: AsyncSession) -> None:
    if query.from_user is None:
        return
    order = await get_order_for_user(session, callback_data.i, query.from_user.id)
    if order is None:
        await query.answer("Order not found", show_alert=True)
        return
    await safe_edit(
        query,
        f"Cancel {html_escape(order.public_id)}?\nUnused quantity is refunded when the provider allows it.",
        cancel_confirm_kb(order.public_id),
    )


@router.callback_query(OrdCB.filter(F.a == "xx"))
async def cb_order_cancel_go(
    query: CallbackQuery, callback_data: OrdCB, session: AsyncSession, settings: Settings
) -> None:
    if query.from_user is None:
        return
    order = await get_order_for_user(session, callback_data.i, query.from_user.id)
    if order is None:
        await query.answer("Order not found", show_alert=True)
        return
    user = await ensure_user(session, query.from_user, settings)
    try:
        order = await request_cancel(session, order, user)
    except (OrderError, PermissionError) as exc:
        await query.answer(str(exc), show_alert=True)
        return
    await safe_edit(query, "Canceled.\n\n" + _order_summary(order), order_detail_kb(order))


@router.message(Command("orders"))
async def cmd_orders(message: Message, session: AsyncSession) -> None:
    if message.from_user is None:
        return
    await _render_list(message, session, message.from_user.id, 0)
