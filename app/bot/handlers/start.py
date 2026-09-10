"""
/start handler — policy gate, force-join, main menu.
"""
from __future__ import annotations

from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message

from app.bot.keyboards import main_menu_keyboard
from app.bot.messages import welcome_message
from app.core.logging import get_logger

logger = get_logger(__name__)
router = Router(name="start")


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    """
    Entry point for all users.
    In full implementation:
      1. Get or create platform user
      2. Check policy acceptance version
      3. Check force-join channel membership
      4. Render welcome message with balance from DB
    """
    user = message.from_user
    if user is None:
        return

    # Placeholder welcome — real impl loads balance from DB
    from decimal import Decimal
    text = welcome_message(
        first_name=user.first_name or "there",
        balance=Decimal("0.00"),
    )
    await message.answer(text, reply_markup=main_menu_keyboard(), parse_mode="HTML")
    logger.info("cmd_start", telegram_id=user.id)
