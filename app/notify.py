"""Best-effort Telegram notifications from background code (workers, webhooks).

The bot instance is registered once at startup. Every call swallows errors: a failed
notification must never break a payment credit or an order sync.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("falaron.notify")

_bot: Any = None
_owner_id: int | None = None


def register(bot: Any, owner_id: int) -> None:
    global _bot, _owner_id
    _bot = bot
    _owner_id = owner_id


async def notify_user(user_id: int, text: str) -> bool:
    if _bot is None:
        return False
    try:
        await _bot.send_message(user_id, text[:4000], parse_mode="HTML", disable_web_page_preview=True)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("notify_user(%s) failed: %s", user_id, type(exc).__name__)
        return False


async def notify_admins(text: str) -> bool:
    if _owner_id is None:
        return False
    return await notify_user(_owner_id, text)
