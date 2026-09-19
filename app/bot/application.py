"""
Customer bot application factory.
Registers all 5 handler routers + middleware.
Section 4, 5, 13, 15, 40 of blueprint.
"""
from __future__ import annotations
from app.core.logging import get_logger

logger = get_logger(__name__)


async def create_dispatcher():
    from aiogram import Dispatcher
    from aiogram.fsm.storage.redis import RedisStorage
    from app.core.redis import get_redis

    try:
        storage = RedisStorage(redis=get_redis())
    except Exception:
        from aiogram.fsm.storage.memory import MemoryStorage
        storage = MemoryStorage()
        logger.warning("Redis unavailable — using MemoryStorage")

    dp = Dispatcher(storage=storage)

    # Middleware stack
    from app.bot.middlewares import register_middlewares
    register_middlewares(dp)

    # Routers — order matters: start must be first (handles HOME callback)
    from app.bot.handlers.start    import router as start_router
    from app.bot.handlers.services import router as services_router
    from app.bot.handlers.orders   import router as orders_router
    from app.bot.handlers.deposit  import router as deposit_router

    dp.include_router(start_router)
    dp.include_router(services_router)
    dp.include_router(orders_router)
    dp.include_router(deposit_router)

    logger.info("customer_bot_dispatcher_created")
    return dp


def create_bot():
    from aiogram import Bot
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode
    from app.core.config import settings
    return Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


async def set_webhook(bot, webhook_url: str, secret: str) -> None:
    await bot.set_webhook(
        url=webhook_url,
        secret_token=secret,
        drop_pending_updates=False,
        allowed_updates=["message", "callback_query"],
    )
    logger.info("customer_bot_webhook_set", url=webhook_url)


async def delete_webhook(bot) -> None:
    await bot.delete_webhook()
    logger.info("customer_bot_webhook_deleted")
