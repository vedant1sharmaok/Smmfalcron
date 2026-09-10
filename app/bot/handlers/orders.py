"""Order handlers — quantity suggestions, FSM order flow, order list."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

router = Router(name="orders")


class OrderStates(StatesGroup):
    selecting_qty  = State()
    entering_link  = State()
    confirming     = State()


def _quantity_suggestions(min_qty: int, max_qty: int) -> list[int]:
    """
    Generate 1-4 quantity suggestion buttons between min and max.
    Always starts at min_qty.
    """
    if min_qty == max_qty:
        return [min_qty]

    candidates = [min_qty]
    mid1 = min_qty * 5
    mid2 = min_qty * 10
    if min_qty < mid1 <= max_qty:
        candidates.append(mid1)
    if min_qty < mid2 <= max_qty and mid2 != mid1:
        candidates.append(mid2)
    if max_qty not in candidates:
        candidates.append(max_qty)

    return sorted(set(candidates))[:4]


@router.callback_query(F.data.startswith("svc_detail:"))
async def show_service_detail(call: CallbackQuery, state: FSMContext):
    from app.bot.keyboards import CB
    public_id = call.data.split(":", 1)[1]
    await state.update_data(public_id=public_id)
    await call.answer()
    # In full impl: load service from DB and render detail card
    await call.message.edit_text(
        f"Service: <code>{public_id}</code>\n\nEnter quantity:",
        parse_mode="HTML",
    )
    await state.set_state(OrderStates.selecting_qty)


@router.callback_query(F.data.startswith("ord_confirm:"))
async def order_confirm_entry(call: CallbackQuery, state: FSMContext):
    await call.answer()
    data = await state.get_data()
    await call.message.edit_text(
        "✅ Confirm your order?\n\nTap Confirm to proceed.",
        parse_mode="HTML",
    )
    await state.set_state(OrderStates.confirming)


@router.callback_query(F.data == "ord_list")
async def orders_list(call: CallbackQuery):
    await call.answer()
    await call.message.edit_text("📋 <b>Your Orders</b>\n\nNo orders yet.", parse_mode="HTML")
