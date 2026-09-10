"""Safe message send/edit helpers — handles all Telegram error types."""
from __future__ import annotations

from typing import Optional

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup, Message

from app.core.logging import get_logger

logger = get_logger(__name__)

_MAX_LEN = 4096


def _truncate(text: str, max_len: int = _MAX_LEN) -> str:
    """Truncate text to max_len bytes, appending '...' if truncated."""
    if len(text) <= max_len:
        return text
    return text[:max_len - 3] + "..."


async def safe_edit(
    message: Message,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    parse_mode: str = "HTML",
) -> Optional[Message]:
    """
    Edit an existing message. Falls back to sending a new message
    if the edit fails (message too old, not modified, etc.).
    Returns the message object or None on ForbiddenError.
    """
    text = _truncate(text)
    try:
        return await message.edit_text(
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )
    except TelegramBadRequest as e:
        if "message is not modified" in str(e).lower():
            return message   # already has correct content
        # Edit failed — fall through to send
    except TelegramRetryAfter as e:
        import asyncio
        await asyncio.sleep(e.retry_after)
        return await safe_edit(message, text, reply_markup, parse_mode)
    except TelegramForbiddenError:
        logger.warning("safe_edit_forbidden", chat_id=message.chat.id)
        return None
    except Exception as exc:
        logger.warning("safe_edit_error", error=str(exc))

    return await safe_send(message, text, reply_markup, parse_mode)


async def safe_send(
    message: Message,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    parse_mode: str = "HTML",
) -> Optional[Message]:
    """
    Send a new message. Returns None if the user has blocked the bot
    or any unrecoverable error occurs.
    """
    text = _truncate(text)
    try:
        return await message.answer(
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )
    except TelegramForbiddenError:
        logger.warning("safe_send_forbidden", chat_id=message.chat.id)
        return None
    except TelegramRetryAfter as e:
        import asyncio
        await asyncio.sleep(e.retry_after)
        return await safe_send(message, text, reply_markup, parse_mode)
    except TelegramBadRequest as exc:
        logger.warning("safe_send_bad_request", error=str(exc))
        return None
    except Exception as exc:
        logger.error("safe_send_error", error=str(exc))
        return None
