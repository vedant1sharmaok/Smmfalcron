"""Category → service → details → dynamic order form → confirmation → debit."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.ads import apply_ad
from app.bot.handlers.start import ensure_user, main_menu_text, menu_kb_for
from app.bot.menus import (
    CatCB,
    MenuCB,
    SvcCB,
    html_escape,
    categories_kb,
    order_confirm_kb,
    safe_edit,
    service_detail_kb,
    services_kb,
)
from app.catalog import get_service, list_categories, list_services
from app.config import Settings
from app.models import Service
from app.orders import OrderError, place_order, status_label
from app.pricing import paise_to_rupees_str, quote_order
from app.wallet import InsufficientFunds, snapshot_of

router = Router(name="services")


class OrderForm(StatesGroup):
    link = State()
    qty = State()
    comments = State()
    mentions = State()
    coupon = State()
    confirm = State()


def _form_needs(service: Service) -> tuple[bool, bool]:
    stype = (service.service_type or "default").lower()
    return stype in {"comments", "custom_comments"}, stype in {"mentions", "comment_mentions"}


async def _show_categories(target: CallbackQuery | Message, session: AsyncSession) -> None:
    cats = await list_categories(session)
    if not cats:
        await safe_edit(target, "No categories are available yet.", None)
        return
    text, markup = await apply_ad(
        session, "services", "<b>Services</b>\nChoose a platform.", categories_kb(cats)
    )
    await safe_edit(target, text, markup)


@router.callback_query(MenuCB.filter(F.a == "services"))
async def cb_services(
    query: CallbackQuery, session: AsyncSession, state: FSMContext
) -> None:
    await state.clear()
    await _show_categories(query, session)


@router.callback_query(CatCB.filter())
async def cb_category(
    query: CallbackQuery, callback_data: CatCB, session: AsyncSession, state: FSMContext
) -> None:
    await state.clear()
    services = await list_services(session, callback_data.i)
    if not services:
        await safe_edit(query, "No services in this category.", categories_kb(await list_categories(session)))
        return
    cat_name = services[0].category.name if services[0].category else "Services"
    text, markup = await apply_ad(
        session,
        "services",
        f"<b>{html_escape(cat_name)}</b>\nSelect a service.",
        services_kb(callback_data.i, services),
    )
    await safe_edit(query, text, markup)


async def _service_text(session: AsyncSession, service: Service, user_id: int) -> str:
    quote = await quote_order(session, service_id=service.id, quantity=service.min_qty, user_id=user_id)
    refill = "Yes" if service.is_refillable else "No"
    cancel = "Yes" if service.is_cancelable else "No"
    desc = html_escape(service.description or "")
    reseller_line = ""
    if quote.reseller_discount_paise:
        reseller_line = f"\nReseller saving: {paise_to_rupees_str(quote.reseller_discount_paise)} at min qty"
    return (
        f"<b>{html_escape(service.name)}</b>\n"
        f"{desc}\n\n"
        f"Type: {html_escape(service.service_type)}\n"
        f"Quantity: {service.min_qty:,} – {service.max_qty:,}\n"
        f"Rate: <b>{paise_to_rupees_str(quote.sell_per_1000_paise)}</b> / 1,000\n"
        f"Avg. start: {html_escape(service.average_time or 'varies')}\n"
        f"Refill: {refill} · Cancel: {cancel}\n"
        f"Min order: {paise_to_rupees_str(quote.charge_paise)} at {service.min_qty:,} qty"
        f"{reseller_line}"
    )


@router.callback_query(SvcCB.filter(F.a == "v"))
async def cb_service_view(
    query: CallbackQuery, callback_data: SvcCB, session: AsyncSession, state: FSMContext
) -> None:
    await state.clear()
    service = await get_service(session, callback_data.i)
    if service is None or not service.is_active:
        await query.answer("Service unavailable", show_alert=True)
        return
    text = await _service_text(session, service, query.from_user.id if query.from_user else 0)
    await safe_edit(query, text, service_detail_kb(service))


@router.callback_query(SvcCB.filter(F.a == "o"))
async def cb_service_order(
    query: CallbackQuery, callback_data: SvcCB, session: AsyncSession, state: FSMContext
) -> None:
    service = await get_service(session, callback_data.i)
    if service is None or not service.is_active:
        await query.answer("Service unavailable", show_alert=True)
        return
    await state.clear()
    await state.update_data(service_id=service.id)
    await state.set_state(OrderForm.link)
    await safe_edit(
        query,
        f"<b>{html_escape(service.name)}</b>\n\n"
        "Send the target link (or @username for profile-based services).\n"
        "Send /cancel to abort.",
        None,
    )


@router.message(OrderForm.link)
async def form_link(message: Message, state: FSMContext, session: AsyncSession) -> None:
    if not message.text or message.text.startswith("/"):
        if message.text and message.text.startswith("/cancel"):
            await state.clear()
            await message.answer("Order cancelled.")
            return
        await message.answer("Please send a link, or /cancel.")
        return
    await state.update_data(link=message.text.strip())
    data = await state.get_data()
    service = await get_service(session, data["service_id"])
    if service is None:
        await state.clear()
        await message.answer("Service no longer available.")
        return
    await state.set_state(OrderForm.qty)
    await message.answer(
        f"Quantity? Min {service.min_qty:,} · Max {service.max_qty:,}",
    )


@router.message(OrderForm.qty)
async def form_qty(message: Message, state: FSMContext, session: AsyncSession) -> None:
    raw = (message.text or "").replace(",", "").strip()
    if raw.startswith("/cancel"):
        await state.clear()
        await message.answer("Order cancelled.")
        return
    try:
        qty = int(raw)
    except ValueError:
        await message.answer("Send a whole number for quantity.")
        return
    data = await state.get_data()
    service = await get_service(session, data["service_id"])
    if service is None:
        await state.clear()
        await message.answer("Service no longer available.")
        return
    if qty < service.min_qty or qty > service.max_qty:
        await message.answer(f"Quantity must be between {service.min_qty:,} and {service.max_qty:,}.")
        return
    await state.update_data(quantity=qty)
    needs_comments, needs_mentions = _form_needs(service)
    if needs_comments:
        await state.set_state(OrderForm.comments)
        await message.answer("Send the comments, one per line.")
        return
    if needs_mentions:
        await state.set_state(OrderForm.mentions)
        await message.answer("Send usernames to mention, separated by spaces or commas.")
        return
    await state.set_state(OrderForm.coupon)
    await message.answer("Coupon code? Send the code, or type <b>skip</b>.", parse_mode="HTML")


@router.message(OrderForm.comments)
async def form_comments(message: Message, state: FSMContext, session: AsyncSession) -> None:
    if not message.text or message.text.startswith("/cancel"):
        await state.clear()
        await message.answer("Order cancelled.")
        return
    await state.update_data(comments=message.text)
    data = await state.get_data()
    service = await get_service(session, data["service_id"])
    if service and _form_needs(service)[1]:
        await state.set_state(OrderForm.mentions)
        await message.answer("Send usernames to mention, separated by spaces or commas.")
        return
    await state.set_state(OrderForm.coupon)
    await message.answer("Coupon code? Send the code, or type <b>skip</b>.", parse_mode="HTML")


@router.message(OrderForm.mentions)
async def form_mentions(message: Message, state: FSMContext) -> None:
    if not message.text or message.text.startswith("/cancel"):
        await state.clear()
        await message.answer("Order cancelled.")
        return
    await state.update_data(mentions=message.text)
    await state.set_state(OrderForm.coupon)
    await message.answer("Coupon code? Send the code, or type <b>skip</b>.", parse_mode="HTML")


@router.message(OrderForm.coupon)
async def form_coupon(
    message: Message, state: FSMContext, session: AsyncSession, settings: Settings
) -> None:
    if message.from_user is None:
        return
    if message.text and message.text.startswith("/cancel"):
        await state.clear()
        await message.answer("Order cancelled.")
        return
    raw = (message.text or "").strip()
    coupon = None if raw.lower() in {"skip", "-", "none", "no"} else raw
    data = await state.get_data()
    extra = {}
    if data.get("comments"):
        extra["comments"] = data["comments"]
    if data.get("mentions"):
        extra["mentions"] = data["mentions"]
    try:
        quote = await quote_order(
            session,
            service_id=data["service_id"],
            quantity=int(data["quantity"]),
            user_id=message.from_user.id,
            coupon_code=coupon,
        )
    except ValueError as exc:
        await message.answer(str(exc) + "\nSend another code, or type skip.")
        return
    user = await ensure_user(session, message.from_user, settings)
    snap = snapshot_of(user)
    service = await get_service(session, data["service_id"])
    name = html_escape(service.name if service else data["service_id"])
    discount_line = (
        f"Discount ({html_escape(quote.coupon_code or '')}): -{paise_to_rupees_str(quote.discount_paise)}\n"
        if quote.discount_paise
        else ""
    )
    reseller_line = (
        f"Reseller saving: -{paise_to_rupees_str(quote.reseller_discount_paise)}\n"
        if quote.reseller_discount_paise
        else ""
    )
    await state.update_data(
        coupon=quote.coupon_code,
        extra=extra,
        quoted_charge=quote.charge_paise,
        confirm_token=data["service_id"][:20],
    )
    await state.set_state(OrderForm.confirm)
    text = (
        f"<b>Confirm order</b>\n\n"
        f"Service: {name}\n"
        f"Target: {html_escape(str(data.get('link')))}\n"
        f"Quantity: {int(data['quantity']):,}\n"
        f"Rate: {paise_to_rupees_str(quote.sell_per_1000_paise)} / 1,000\n"
        f"Subtotal: {paise_to_rupees_str(quote.subtotal_paise)}\n"
        f"{reseller_line}"
        f"{discount_line}"
        f"<b>Charge: {paise_to_rupees_str(quote.charge_paise)}</b>\n"
        f"Wallet available: {paise_to_rupees_str(snap.available_paise)}\n\n"
        "Prices are calculated on the server. Confirm to debit your wallet."
    )
    token = data["service_id"]
    await message.answer(text, reply_markup=order_confirm_kb(token), parse_mode="HTML")


@router.callback_query(SvcCB.filter(F.a == "ok"))
async def cb_confirm_order(
    query: CallbackQuery,
    callback_data: SvcCB,
    session: AsyncSession,
    settings: Settings,
    state: FSMContext,
) -> None:
    if query.from_user is None:
        return
    current = await state.get_state()
    if current != OrderForm.confirm.state:
        await query.answer("This confirmation expired. Start again.", show_alert=True)
        return
    data = await state.get_data()
    if data.get("service_id") != callback_data.i:
        await query.answer("Mismatched confirmation.", show_alert=True)
        return
    user = await ensure_user(session, query.from_user, settings)
    try:
        order = await place_order(
            session,
            user,
            service_id=data["service_id"],
            link=str(data["link"]),
            quantity=int(data["quantity"]),
            extra=data.get("extra") or {},
            coupon_code=data.get("coupon"),
        )
    except InsufficientFunds:
        await state.clear()
        await safe_edit(
            query,
            "Insufficient balance. Deposit funds and try again.",
            await menu_kb_for(session, user, settings),
        )
        return
    except (OrderError, PermissionError, ValueError) as exc:
        await state.clear()
        await safe_edit(query, f"Could not place order: {html_escape(str(exc))}", None)
        return
    await state.clear()
    await safe_edit(
        query,
        f"<b>Order {html_escape(order.public_id)}</b>\n"
        f"Status: {status_label(order.status)}\n"
        f"Charged: {paise_to_rupees_str(order.charge_paise)}\n"
        f"Provider ref: {html_escape(order.provider_order_id or '—')}\n"
        + (f"\n{html_escape(order.fail_reason)}" if order.fail_reason else ""),
        await menu_kb_for(session, user, settings),
    )


@router.message(F.text.regexp(r"^/cancel$"))
async def cmd_cancel_form(
    message: Message, state: FSMContext, session: AsyncSession, settings: Settings
) -> None:
    current = await state.get_state()
    if current is None:
        return
    await state.clear()
    if message.from_user is None:
        return
    user = await ensure_user(session, message.from_user, settings)
    await message.answer(
        "Cancelled.\n\n" + await main_menu_text(session, user, settings),
        reply_markup=await menu_kb_for(session, user, settings),
        parse_mode="HTML",
    )
