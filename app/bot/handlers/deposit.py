"""Deposit flow and profile card handlers."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery

from app.bot.keyboards import CB, deposit_keyboard, back_to_home_keyboard
from app.core.logging import get_logger

logger = get_logger(__name__)
router = Router(name="deposit")


@router.callback_query(F.data == CB.WALLET)
async def show_wallet(call: CallbackQuery) -> None:
    """Show wallet balance and deposit options."""
    await call.answer()
    # Full impl: load balance from DB
    from decimal import Decimal
    balance = Decimal("0.00")
    await call.message.edit_text(
        f"💳 <b>Wallet</b>\n\nBalance: <b>₹{balance:.2f}</b>\n\nSelect amount to deposit:",
        parse_mode="HTML",
        reply_markup=deposit_keyboard(),
    )


@router.callback_query(F.data.startswith("dep_amt:"))
async def handle_deposit_amount(call: CallbackQuery) -> None:
    """Handle preset deposit amount selection."""
    await call.answer()
    amount = call.data.split(":")[1]
    # Full impl: create payment intent via Razorpay and send payment link
    await call.message.edit_text(
        f"💳 Deposit ₹{amount}\n\n"
        f"A payment link will be sent shortly.",
        parse_mode="HTML",
        reply_markup=back_to_home_keyboard(),
    )


@router.callback_query(F.data == CB.PROFILE)
async def show_profile(call: CallbackQuery) -> None:
    """Show user profile card."""
    await call.answer()
    from decimal import Decimal
    from app.bot.messages import profile_card
    user = call.from_user
    text = profile_card(
        first_name=user.first_name or "User",
        telegram_id=user.id,
        balance=Decimal("0.00"),
        total_orders=0,
        is_premium=False,
    )
    await call.message.edit_text(
        text, parse_mode="HTML", reply_markup=back_to_home_keyboard()
    )
