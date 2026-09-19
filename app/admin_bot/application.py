"""
Admin bot application factory.
Section 20, 21 of blueprint.

The admin bot runs as a SEPARATE bot from the customer bot.
Requires: ADMIN_BOT_TOKEN env var (different token from BOT_TOKEN)
Access control: ADMIN_TELEGRAM_IDS env var (comma-separated Telegram IDs)

If ADMIN_BOT_TOKEN is not set, the admin bot is silently disabled.
"""
from __future__ import annotations
from app.core.logging import get_logger

logger = get_logger(__name__)


async def create_admin_bot_and_dispatcher():
    """Create admin bot and dispatcher. Returns (bot, dp) or (None, None)."""
    import os
    admin_token = os.environ.get("ADMIN_BOT_TOKEN", "")
    if not admin_token:
        logger.warning("ADMIN_BOT_TOKEN not set — admin bot disabled")
        return None, None

    from aiogram import Bot, Dispatcher
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode
    from aiogram.fsm.storage.redis import RedisStorage
    from app.core.redis import get_redis

    bot = Bot(
        token=admin_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    try:
        storage = RedisStorage(redis=get_redis())
    except Exception:
        from aiogram.fsm.storage.memory import MemoryStorage
        storage = MemoryStorage()
        logger.warning("Redis unavailable — admin bot using MemoryStorage (not persistent)")

    dp = Dispatcher(storage=storage)

    from app.admin_bot.handlers import router as admin_router
    dp.include_router(admin_router)

    logger.info("admin_bot_created")
    return bot, dp


async def start_admin_bot_polling(bot, dp) -> None:
    """Start admin bot in polling mode (for dev/testing)."""
    if bot is None:
        return
    logger.info("admin_bot_polling_start")
    await dp.start_polling(bot)


async def set_admin_webhook(bot, base_url: str) -> None:
    """Set admin bot webhook."""
    if bot is None:
        return
    import os
    secret = os.environ.get("ADMIN_BOT_WEBHOOK_SECRET", "")
    url    = f"{base_url.rstrip('/')}/admin-bot/webhook"
    await bot.set_webhook(url=url, secret_token=secret)
    logger.info("admin_bot_webhook_set", url=url)
