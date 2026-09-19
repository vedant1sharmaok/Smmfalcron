"""
Customer notification service.
Sends bot messages to customers on order/payment status changes.
Section 14, 15, 32 of blueprint.
"""
from __future__ import annotations
from decimal import Decimal
from typing import Optional
from app.core.logging import get_logger

logger = get_logger(__name__)

_bot = None

def set_bot(bot) -> None:
    global _bot
    _bot = bot


async def _send(telegram_id: int, text: str) -> None:
    if not _bot:
        return
    try:
        await _bot.send_message(chat_id=telegram_id, text=text, parse_mode="HTML")
    except Exception as exc:
        logger.debug("notification_send_failed", telegram_id=telegram_id, error=str(exc))


async def notify_order_placed(telegram_id: int, public_ref: str, service_name: str, qty: int, price: Decimal) -> None:
    await _send(telegram_id,
        f"✅ <b>Order Placed!</b>\n\n"
        f"Ref: <code>{public_ref}</code>\n"
        f"Service: {service_name}\n"
        f"Qty: {qty:,}  •  ₹{price:.2f}\n\n"
        f"We'll notify you when it's complete."
    )


async def notify_order_completed(telegram_id: int, public_ref: str) -> None:
    await _send(telegram_id,
        f"🟢 <b>Order Completed!</b>\n\nRef: <code>{public_ref}</code>"
    )


async def notify_order_partial(telegram_id: int, public_ref: str, remains: int) -> None:
    await _send(telegram_id,
        f"🟠 <b>Order Partial</b>\n\n"
        f"Ref: <code>{public_ref}</code>\n"
        f"Remains: {remains:,}\n\n"
        f"A partial refund will be issued where applicable."
    )


async def notify_order_failed(telegram_id: int, public_ref: str, refunded: bool) -> None:
    refund_note = "\n\n💸 Your balance has been refunded." if refunded else ""
    await _send(telegram_id,
        f"🔴 <b>Order Failed</b>\n\nRef: <code>{public_ref}</code>{refund_note}"
    )


async def notify_deposit_success(telegram_id: int, amount: Decimal, new_balance: Decimal) -> None:
    await _send(telegram_id,
        f"💳 <b>Deposit Successful!</b>\n\n"
        f"Added: <b>₹{amount:.2f}</b>\n"
        f"New balance: <b>₹{new_balance:.2f}</b>"
    )


async def notify_refill_complete(telegram_id: int, public_ref: str) -> None:
    await _send(telegram_id,
        f"♻️ <b>Refill Complete!</b>\n\nOrder: <code>{public_ref}</code>"
    )


async def notify_refill_failed(telegram_id: int, public_ref: str) -> None:
    await _send(telegram_id,
        f"❌ <b>Refill Failed</b>\n\nOrder: <code>{public_ref}</code>\nPlease contact support."
    )
