"""Bot routers."""

from aiogram import Router

from app.bot.handlers.admin import router as admin_router
from app.bot.handlers.orders import router as orders_router
from app.bot.handlers.profile import router as profile_router
from app.bot.handlers.promoter import router as promoter_router
from app.bot.handlers.reseller import router as reseller_router
from app.bot.handlers.rewards import router as rewards_router
from app.bot.handlers.services import router as services_router
from app.bot.handlers.start import router as start_router
from app.bot.handlers.stats import router as stats_router
from app.bot.handlers.wallet import router as wallet_router


def setup_routers() -> Router:
    root = Router(name="root")
    root.include_router(start_router)
    root.include_router(services_router)
    root.include_router(orders_router)
    root.include_router(wallet_router)
    root.include_router(profile_router)
    root.include_router(stats_router)
    root.include_router(rewards_router)
    root.include_router(reseller_router)
    root.include_router(promoter_router)
    root.include_router(admin_router)
    return root
