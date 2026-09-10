"""Service browser handlers — categories → services → detail."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from app.bot.keyboards import CB, back_to_home_keyboard
from app.core.logging import get_logger

logger = get_logger(__name__)
router = Router(name="services")


@router.callback_query(F.data == CB.CAT_LIST)
async def show_categories(call: CallbackQuery) -> None:
    """Show all active service categories."""
    await call.answer()
    # Full impl: load categories from DB and render categories_keyboard
    await call.message.edit_text(
        "📦 <b>Categories</b>\n\nLoading services…",
        parse_mode="HTML",
        reply_markup=back_to_home_keyboard(),
    )


@router.callback_query(F.data.startswith("svc_list:"))
async def show_service_list(call: CallbackQuery) -> None:
    """Show paginated service list for a category."""
    await call.answer()
    parts = call.data.split(":")
    cat_id = parts[1] if len(parts) > 1 else ""
    page   = int(parts[2]) if len(parts) > 2 else 1
    # Full impl: load services filtered by cat_id + page from DB
    await call.message.edit_text(
        f"📦 <b>Services</b>\n\nCategory: {cat_id} | Page {page}",
        parse_mode="HTML",
        reply_markup=back_to_home_keyboard(),
    )


@router.callback_query(F.data == CB.HOME)
async def go_home(call: CallbackQuery, state: FSMContext) -> None:
    """Return to main menu from anywhere."""
    await call.answer()
    await state.clear()
    from decimal import Decimal
    from app.bot.messages import welcome_message
    from app.bot.keyboards import main_menu_keyboard
    user = call.from_user
    text = welcome_message(
        first_name=user.first_name or "there",
        balance=Decimal("0.00"),
    )
    await call.message.edit_text(text, reply_markup=main_menu_keyboard(), parse_mode="HTML")
