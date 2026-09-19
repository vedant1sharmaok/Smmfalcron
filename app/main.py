"""
FastAPI application — customer bot webhook, admin bot webhook,
MiniApp API, payment webhooks, metrics, health.
Section 3, 4, 18-19, 20 of blueprint.
"""
from __future__ import annotations
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from app.core.config import settings
from app.core.logging import configure_logging, get_logger

logger = get_logger(__name__)

# Module-level bot/dispatcher references (set during lifespan)
_customer_bot = None
_customer_dp  = None
_admin_bot    = None
_admin_dp     = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _customer_bot, _customer_dp, _admin_bot, _admin_dp

    configure_logging(settings.log_level)

    # Redis
    from app.core.redis import init_redis
    await init_redis()

    # Provider registry
    from app.providers.registry import registry
    from app.core.database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        count = await registry.load_providers(db)
        logger.info("providers_loaded", count=count)

    # Customer bot
    from app.bot.application import create_bot, create_dispatcher, set_webhook
    _customer_bot = create_bot()
    _customer_dp  = await create_dispatcher()

    if settings.bot_webhook_url:
        await set_webhook(_customer_bot, settings.bot_webhook_url, settings.bot_webhook_secret)
        logger.info("customer_bot_webhook_set")

    # Wire Telegram operational logger to the customer bot
    try:
        from app.notifications.telegram_logger import tg_logger
        from app.notifications.service import set_bot as set_notif_bot
        tg_logger._set_bot(_customer_bot)
        set_notif_bot(_customer_bot)
    except Exception:
        pass

    # Admin bot (optional — requires ADMIN_BOT_TOKEN)
    if settings.admin_bot_token:
        from app.admin_bot.application import create_admin_bot_and_dispatcher
        _admin_bot, _admin_dp = await create_admin_bot_and_dispatcher()
        if _admin_bot and settings.domain:
            from app.admin_bot.application import set_admin_webhook
            await set_admin_webhook(_admin_bot, settings.domain)

    logger.info("app_started", env=settings.app_env)
    yield

    # Shutdown
    from app.core.redis import close_redis
    await registry.shutdown()
    if _customer_bot:
        await _customer_bot.session.close()
    if _admin_bot:
        await _admin_bot.session.close()
    await close_redis()
    logger.info("app_stopped")


def create_app() -> FastAPI:
    app = FastAPI(
        title="SMM Falcron API",
        version="2.0.0",
        docs_url="/docs" if settings.app_env != "production" else None,
        redoc_url=None,
        lifespan=lifespan,
    )

    # CORS for Mini App
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["https://web.telegram.org", "https://t.me"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Prometheus middleware
    try:
        from app.monitoring.instrumentation import PrometheusMiddleware
        app.add_middleware(PrometheusMiddleware)
    except Exception:
        pass

    # ── Health ────────────────────────────────────────────────────────────────
    from app.monitoring.health import router as health_router
    app.include_router(health_router)

    # ── MiniApp API ───────────────────────────────────────────────────────────
    from app.miniapp.routes import router as miniapp_router
    app.include_router(miniapp_router)

    # ── Admin REST API ────────────────────────────────────────────────────────
    from app.admin.routes import router as admin_router
    app.include_router(admin_router, prefix="/admin")

    # ── Own SMM API ───────────────────────────────────────────────────────────
    try:
        from app.api.routes import router as api_router
        app.include_router(api_router, prefix="/v2")
    except Exception:
        pass

    # ── Tenant webhook receiver ───────────────────────────────────────────────
    try:
        from app.tenants.routes import router as tenant_router
        app.include_router(tenant_router)
    except Exception:
        pass

    # ── Metrics ───────────────────────────────────────────────────────────────
    @app.get("/metrics")
    async def metrics_endpoint(request: Request):
        try:
            from app.monitoring.instrumentation import metrics_endpoint as me
            return await me(request)
        except Exception:
            return Response(content="# metrics unavailable\n", media_type="text/plain")

    # ── Customer bot webhook ──────────────────────────────────────────────────
    @app.post("/bot/webhook")
    async def customer_bot_webhook(request: Request):
        if _customer_bot is None or _customer_dp is None:
            return Response(content="Bot not initialised", status_code=503)

        secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if settings.bot_webhook_secret and secret != settings.bot_webhook_secret:
            return Response(content="Forbidden", status_code=403)

        import json
        from aiogram.types import Update
        body = await request.body()
        try:
            data   = json.loads(body)
            update = Update(**data)
            await _customer_dp.feed_update(_customer_bot, update)
        except Exception as exc:
            logger.error("customer_webhook_error", error=str(exc))
        return Response(content="ok")

    # ── Admin bot webhook ─────────────────────────────────────────────────────
    @app.post("/admin-bot/webhook")
    async def admin_bot_webhook(request: Request):
        if _admin_bot is None or _admin_dp is None:
            return Response(content="Admin bot not configured", status_code=404)

        secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if settings.admin_bot_webhook_secret and secret != settings.admin_bot_webhook_secret:
            return Response(content="Forbidden", status_code=403)

        import json
        from aiogram.types import Update
        body = await request.body()
        try:
            data   = json.loads(body)
            update = Update(**data)
            await _admin_dp.feed_update(_admin_bot, update)
        except Exception as exc:
            logger.error("admin_webhook_error", error=str(exc))
        return Response(content="ok")

    # ── Payment webhooks ──────────────────────────────────────────────────────
    @app.post("/webhooks/razorpay")
    async def razorpay_webhook(request: Request):
        from app.core.database import AsyncSessionLocal
        from app.payments.adapters import process_payment_webhook
        body    = await request.body()
        headers = dict(request.headers)
        async with AsyncSessionLocal() as db:
            try:
                await process_payment_webhook(db, "razorpay", body, headers)
                await db.commit()
            except Exception as exc:
                logger.error("razorpay_webhook_error", error=str(exc))
        return Response(content="ok")   # always 200 — Razorpay retries on non-200   # always 200 — Razorpay retries on non-200

    @app.post("/webhooks/stripe")
    async def stripe_webhook(request: Request):
        from app.core.database import AsyncSessionLocal
        try:
            from app.payments.stripe_adapter import process_stripe_webhook
            body    = await request.body()
            headers = dict(request.headers)
            async with AsyncSessionLocal() as db:
                await process_stripe_webhook(db, body, headers)
                await db.commit()
        except Exception as exc:
            logger.error("stripe_webhook_error", error=str(exc))
        return Response(content="ok")

    return app


app = create_app()
