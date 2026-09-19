"""
Services handler — full browse flow.
Section 5 of blueprint: Category → Service List → Service Detail → Order Form.
Dynamic order forms generated from service capabilities (Section 5.3).
Callbacks EDIT existing messages (Section 50.19).
"""
from __future__ import annotations
from decimal import Decimal
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from app.bot.keyboards import CB, back_home_keyboard, categories_keyboard, services_keyboard, service_detail_keyboard, qty_suggestions_keyboard, order_confirm_keyboard
from app.bot.messages import categories_header, services_list_header, service_card, order_form_link_prompt, order_form_qty_prompt, order_preview_card, order_submitted, error_card, maintenance_card
from app.bot.safe_send import safe_edit, safe_send
from app.core.logging import get_logger

logger = get_logger(__name__)
router = Router(name="services")

PAGE_SIZE = 8   # services per page


class OrderFSM(StatesGroup):
    entering_link  = State()
    entering_qty   = State()
    entering_custom_qty = State()
    confirming     = State()


def _qty_suggestions(min_qty: int, max_qty: int) -> list[int]:
    """Generate up to 4 quantity presets between min and max."""
    if min_qty == max_qty:
        return [min_qty]
    presets = [min_qty]
    for mult in [5, 10, 50]:
        v = min_qty * mult
        if min_qty < v <= max_qty and v not in presets:
            presets.append(v)
        if len(presets) >= 4:
            break
    if max_qty not in presets:
        presets.append(max_qty)
    return sorted(set(presets))[:4]


async def _check_policy_accepted(db, telegram_id: int) -> bool:
    from sqlalchemy import select
    from app.core.models import User
    result = await db.execute(select(User).where(User.telegram_id == telegram_id))
    user   = result.scalar_one_or_none()
    return bool(user and (user.policy_version or 0) >= 1)


# ── Category list ─────────────────────────────────────────────────────────────

@router.callback_query(F.data == CB.CAT_LIST)
async def show_categories(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.clear()

    from app.core.database import AsyncSessionLocal
    from sqlalchemy import select
    from app.core.models import Category

    async with AsyncSessionLocal() as db:
        rows = list((await db.execute(
            select(Category)
            .where(Category.is_active == True)
            .order_by(Category.sort_order, Category.id)
        )).scalars().all())

    if not rows:
        await safe_edit(call.message,
            "🛍️ <b>Services</b>\n\nNo service categories are currently available. Please check back soon.",
            reply_markup=back_home_keyboard())
        return

    cats = [{"id": c.id, "name": c.name} for c in rows]
    await safe_edit(call.message, categories_header(), reply_markup=categories_keyboard(cats))


# ── Service list ──────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("svc:list:"))
async def show_service_list(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    parts  = call.data.split(":")
    cat_id = int(parts[2]) if len(parts) > 2 else 0
    page   = int(parts[3]) if len(parts) > 3 else 1

    from app.core.database import AsyncSessionLocal
    from sqlalchemy import select, func
    from app.core.models import Service, Category

    async with AsyncSessionLocal() as db:
        cat    = await db.get(Category, cat_id)
        total  = (await db.execute(
            select(func.count()).where(Service.is_active == True, Service.category_id == cat_id, Service.ordering_enabled == True)
        )).scalar_one()
        rows   = list((await db.execute(
            select(Service)
            .where(Service.is_active == True, Service.category_id == cat_id, Service.ordering_enabled == True)
            .order_by(Service.sort_order, Service.id)
            .offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE)
        )).scalars().all())

    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    cat_name    = cat.name if cat else "Services"
    svcs        = [{"public_id": s.public_id, "display_name": s.display_name} for s in rows]

    await safe_edit(
        call.message,
        services_list_header(cat_name, page, total_pages),
        reply_markup=services_keyboard(svcs, cat_id, page, total_pages),
    )


# ── Service detail ────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("svc:detail:"))
async def show_service_detail(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    public_id = call.data.split(":", 2)[2]

    from app.core.database import AsyncSessionLocal
    from sqlalchemy import select
    from app.core.models import Service, Category, ServiceProviderMapping, ProviderService

    async with AsyncSessionLocal() as db:
        svc = (await db.execute(
            select(Service).where(Service.public_id == public_id.upper())
        )).scalar_one_or_none()

        if svc is None or not svc.is_active:
            await safe_edit(call.message, error_card("Service not found."), reply_markup=back_home_keyboard())
            return

        cat = await db.get(Category, svc.category_id)

        # Get provider-level min/max
        mapping = (await db.execute(
            select(ServiceProviderMapping).where(
                ServiceProviderMapping.service_id == svc.id,
                ServiceProviderMapping.is_primary == True,
            )
        )).scalar_one_or_none()

        min_qty = max_qty = 0
        rate    = svc.custom_price or Decimal("0")
        refill  = svc.refill_enabled
        cancel  = svc.cancel_enabled

        if mapping:
            prov_svc = (await db.execute(
                select(ProviderService).where(
                    ProviderService.provider_id == mapping.provider_id,
                    ProviderService.provider_svc_id == mapping.provider_svc_id,
                )
            )).scalar_one_or_none()
            if prov_svc:
                min_qty = prov_svc.min_qty
                max_qty = prov_svc.max_qty
                if not rate:
                    rate = prov_svc.rate
                refill  = prov_svc.refill
                cancel  = prov_svc.cancel

    text = service_card(
        public_id=svc.public_id,
        display_name=svc.display_name,
        category=cat.name if cat else "",
        min_qty=min_qty,
        max_qty=max_qty,
        price_per_1000=rate,
        refill=refill,
        cancel=cancel,
        requires_premium=svc.requires_premium,
    )

    await safe_edit(
        call.message, text,
        reply_markup=service_detail_keyboard(public_id, refill=refill, cancel=cancel),
    )

    # Store service context for order FSM
    await state.update_data(
        public_id=public_id, min_qty=min_qty, max_qty=max_qty,
        price_per_1000=str(rate), service_name=svc.display_name,
    )


# ── Order flow ────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("svc:order:"))
async def start_order(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    public_id = call.data.split(":", 2)[2]
    data      = await state.get_data()

    if data.get("public_id") != public_id:
        # Re-load service data if FSM lost context
        from app.core.database import AsyncSessionLocal
        from sqlalchemy import select
        from app.core.models import Service, ServiceProviderMapping, ProviderService
        async with AsyncSessionLocal() as db:
            svc = (await db.execute(select(Service).where(Service.public_id == public_id.upper()))).scalar_one_or_none()
            if not svc:
                await safe_edit(call.message, error_card("Service not found."), reply_markup=back_home_keyboard()); return
            mapping = (await db.execute(select(ServiceProviderMapping).where(
                ServiceProviderMapping.service_id == svc.id, ServiceProviderMapping.is_primary == True,
            ))).scalar_one_or_none()
            min_qty = max_qty = 0; rate = svc.custom_price or Decimal("0")
            if mapping:
                ps = (await db.execute(select(ProviderService).where(
                    ProviderService.provider_id == mapping.provider_id,
                    ProviderService.provider_svc_id == mapping.provider_svc_id,
                ))).scalar_one_or_none()
                if ps: min_qty = ps.min_qty; max_qty = ps.max_qty; rate = rate or ps.rate
            await state.update_data(public_id=public_id, min_qty=min_qty, max_qty=max_qty,
                                    price_per_1000=str(rate), service_name=svc.display_name)
            data = await state.get_data()

    min_qty  = data.get("min_qty", 0)
    max_qty  = data.get("max_qty", 0)
    rate     = Decimal(str(data.get("price_per_1000", "0")))
    svc_name = data.get("service_name", "Service")

    await safe_edit(
        call.message,
        order_form_link_prompt(svc_name, min_qty, max_qty, rate),
        reply_markup=back_home_keyboard(),
    )
    await state.set_state(OrderFSM.entering_link)


@router.message(OrderFSM.entering_link)
async def receive_link(message: Message, state: FSMContext) -> None:
    link = message.text.strip() if message.text else ""
    if not link or len(link) < 3:
        await message.answer(error_card("Please enter a valid URL or username."))
        return

    await state.update_data(link=link)
    data    = await state.get_data()
    min_qty = data.get("min_qty", 1)
    max_qty = data.get("max_qty", 100000)

    suggestions = _qty_suggestions(min_qty, max_qty)
    await message.answer(
        order_form_qty_prompt(min_qty, max_qty),
        reply_markup=qty_suggestions_keyboard(suggestions, data.get("public_id", "")),
        parse_mode="HTML",
    )
    await state.set_state(OrderFSM.entering_qty)


@router.callback_query(F.data.startswith("ord:qty:"))
async def qty_suggestion_selected(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    parts     = call.data.split(":")
    qty       = int(parts[3]) if len(parts) > 3 else 0
    data      = await state.get_data()
    await state.update_data(quantity=qty)
    await _show_order_preview(call, state, data, qty)


@router.callback_query(F.data.startswith("ord:qty_custom:"))
async def qty_custom(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    data    = await state.get_data()
    min_qty = data.get("min_qty", 1)
    max_qty = data.get("max_qty", 100000)
    await safe_edit(call.message,
        f"Enter a custom quantity:\nMin: <b>{min_qty:,}</b>  Max: <b>{max_qty:,}</b>",
        reply_markup=back_home_keyboard())
    await state.set_state(OrderFSM.entering_custom_qty)


@router.message(OrderFSM.entering_custom_qty)
async def receive_custom_qty(message: Message, state: FSMContext) -> None:
    try:
        qty  = int(message.text.strip())
        data = await state.get_data()
        min_qty = data.get("min_qty", 1)
        max_qty = data.get("max_qty", 100000)
        if qty < min_qty or qty > max_qty:
            await message.answer(error_card(f"Quantity must be between {min_qty:,} and {max_qty:,}."), parse_mode="HTML")
            return
        await state.update_data(quantity=qty)
        await state.set_state(OrderFSM.confirming)

        rate  = Decimal(str(data.get("price_per_1000", "0")))
        price = (rate * qty / 1000).quantize(Decimal("0.01"))
        import uuid as _uuid
        idem  = str(_uuid.uuid4())[:20]
        await state.update_data(idem_key=idem, price=str(price))

        await message.answer(
            order_preview_card(data.get("service_name",""), qty, data.get("link",""), price),
            reply_markup=order_confirm_keyboard(idem),
            parse_mode="HTML",
        )
    except ValueError:
        await message.answer(error_card("Please enter a valid number."), parse_mode="HTML")


async def _show_order_preview(call: CallbackQuery, state: FSMContext, data: dict, qty: int) -> None:
    import uuid as _uuid
    rate  = Decimal(str(data.get("price_per_1000", "0")))
    price = (rate * qty / 1000).quantize(Decimal("0.01"))
    idem  = str(_uuid.uuid4())[:20]
    await state.update_data(quantity=qty, idem_key=idem, price=str(price))
    await state.set_state(OrderFSM.confirming)
    await safe_edit(
        call.message,
        order_preview_card(data.get("service_name",""), qty, data.get("link",""), price),
        reply_markup=order_confirm_keyboard(idem),
    )


@router.callback_query(F.data.startswith("ord:confirm:"))
async def confirm_order(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer("⏳ Placing order...")
    data     = await state.get_data()
    tg_user  = call.from_user
    quantity = data.get("quantity", 0)
    link     = data.get("link", "")
    idem_key = data.get("idem_key", "")
    public_id= data.get("public_id", "")

    from app.core.database import AsyncSessionLocal
    from sqlalchemy import select
    from app.core.models import Service, User

    async with AsyncSessionLocal() as db:
        user = (await db.execute(select(User).where(User.telegram_id == tg_user.id))).scalar_one_or_none()
        if not user:
            await safe_edit(call.message, error_card("Session expired. Send /start to continue."), reply_markup=back_home_keyboard()); return

        svc = (await db.execute(select(Service).where(Service.public_id == public_id.upper()))).scalar_one_or_none()
        if not svc:
            await safe_edit(call.message, error_card("Service not found."), reply_markup=back_home_keyboard()); return

        try:
            from app.orders.engine import create_order
            order = await create_order(
                db=db, user_id=user.id, service_id=svc.id,
                quantity=quantity, link=link,
                idempotency_key=idem_key, source="bot",
            )
            await db.commit()
        except Exception as exc:
            logger.warning("order_create_failed", user_id=user.id, error=str(exc))
            await safe_edit(call.message, error_card(str(exc)[:200]), reply_markup=back_home_keyboard())
            return

    await state.clear()
    await safe_edit(
        call.message,
        order_submitted(order.public_ref, svc.display_name, quantity),
        reply_markup=back_home_keyboard(),
    )
