"""Bot application factory — webhook and polling modes."""
from __future__ import annotations

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.redis import RedisStorage

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)


def create_bot() -> Bot:
    return Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


async def create_dispatcher() -> Dispatcher:
    from app.core.redis import get_redis
    storage = RedisStorage(redis=get_redis())
    dp      = Dispatcher(storage=storage)

    # Middleware
    from app.bot.middlewares import register_middlewares
    register_middlewares(dp)

    # Routers
    from app.bot.handlers.start    import router as start_router
    from app.bot.handlers.services import router as services_router
    from app.bot.handlers.orders   import router as orders_router
    from app.bot.handlers.deposit  import router as deposit_router

    dp.include_router(start_router)
    dp.include_router(services_router)
    dp.include_router(orders_router)
    dp.include_router(deposit_router)

    return dp


async def set_webhook(bot: Bot) -> None:
    await bot.set_webhook(
        url=settings.bot_webhook_url,
        secret_token=settings.bot_webhook_secret,
        drop_pending_updates=False,
        allowed_updates=["message", "callback_query"],
    )
    logger.info("bot_webhook_set", url=settings.bot_webhook_url)


async def delete_webhook(bot: Bot) -> None:
    await bot.delete_webhook()
    logger.info("bot_webhook_deleted")
