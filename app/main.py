"""FastAPI application factory."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.core.logging import configure_logging, get_logger

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown lifecycle."""
    configure_logging(settings.log_level)
    from app.core.redis import init_redis
    from app.providers.registry import registry
    from app.core.database import AsyncSessionLocal

    await init_redis()

    async with AsyncSessionLocal() as db:
        count = await registry.load_providers(db)
        logger.info("providers_loaded", count=count)

    logger.info("app_started", env=settings.app_env)
    yield

    from app.core.redis import close_redis
    await registry.shutdown()
    await close_redis()
    logger.info("app_stopped")


def create_app() -> FastAPI:
    app = FastAPI(
        title="SMM Platform API",
        version="1.0.0",
        docs_url="/docs" if settings.app_env != "production" else None,
        redoc_url=None,
        lifespan=lifespan,
    )

    # ── CORS ──────────────────────────────────────────────────────────────────
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["https://web.telegram.org"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Prometheus middleware ──────────────────────────────────────────────────
    from app.monitoring.instrumentation import PrometheusMiddleware
    app.add_middleware(PrometheusMiddleware)

    # ── Routers ───────────────────────────────────────────────────────────────
    from app.monitoring.health   import router as health_router
    from app.miniapp.routes      import router as miniapp_router
    from app.monitoring.instrumentation import metrics_endpoint

    app.include_router(health_router)
    app.include_router(miniapp_router)

    # Admin routes
    try:
        from app.admin.routes import router as admin_router
        app.include_router(admin_router, prefix="/admin")
    except ImportError:
        logger.warning("admin_routes_not_available")

    # Own SMM API
    try:
        from app.api.routes import router as api_router
        app.include_router(api_router, prefix="/v2")
    except ImportError:
        logger.warning("api_routes_not_available")

    # Tenant webhook receiver
    try:
        from app.tenants.routes import bot_webhook
        app.include_router(bot_webhook)
    except ImportError:
        pass

    # Metrics
    app.add_api_route("/metrics", metrics_endpoint, methods=["GET"])

    # Bot webhook
    @app.post("/bot/webhook")
    async def bot_webhook_handler(request):
        from aiogram.types import Update
        from app.bot.application import create_bot, create_dispatcher
        # Full impl: feed update to dispatcher
        return {"ok": True}

    # Payment webhooks
    @app.post("/webhooks/razorpay")
    async def razorpay_webhook(request):
        from app.core.database import AsyncSessionLocal
        from app.payments.adapters import process_payment_webhook
        body    = await request.body()
        headers = dict(request.headers)
        async with AsyncSessionLocal() as db:
            try:
                await process_payment_webhook(db, "razorpay", body, headers)
                await db.commit()
            except Exception as e:
                logger.error("razorpay_webhook_error", error=str(e))
        return {"ok": True}

    @app.post("/webhooks/stripe")
    async def stripe_webhook(request):
        from app.core.database import AsyncSessionLocal
        from app.payments.stripe_adapter import process_stripe_webhook
        body    = await request.body()
        headers = dict(request.headers)
        async with AsyncSessionLocal() as db:
            try:
                await process_stripe_webhook(db, body, headers)
                await db.commit()
            except Exception as e:
                logger.error("stripe_webhook_error", error=str(e))
        return {"ok": True}

    return app


app = create_app()
