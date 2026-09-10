"""
FastAPI application factory.

SMM Platform:
- FastAPI API
- Telegram Mini App
- Main Telegram bot webhook
- Multi-tenant Telegram bot webhooks
- Provider registry
- Redis
- PostgreSQL
- Admin API
- Payment webhooks
- Prometheus metrics
- Static React/Vite Mini App
"""

from __future__ import annotations

import hmac
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.core.config import settings
from app.core.logging import configure_logging, get_logger

logger = get_logger(__name__)


# ============================================================================
# APPLICATION LIFESPAN
# ============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Startup and shutdown lifecycle.

    Keeps the existing provider/Redis lifecycle and additionally starts:
      1. Main SMM Telegram bot
      2. Main bot dispatcher
      3. Main bot webhook
      4. Tenant bot manager
    """

    configure_logging(settings.log_level)

    # ------------------------------------------------------------------------
    # Core services
    # ------------------------------------------------------------------------
    from app.core.redis import init_redis
    from app.core.database import AsyncSessionLocal
    from app.providers.registry import registry

    await init_redis()

    # Load provider configuration
    async with AsyncSessionLocal() as db:
        count = await registry.load_providers(db)
        logger.info(
            "providers_loaded",
            count=count,
        )

    # ------------------------------------------------------------------------
    # Main Telegram bot
    # ------------------------------------------------------------------------
    main_bot = None
    main_dispatcher = None

    if settings.bot_token:
        try:
            from app.bot.application import (
                create_bot,
                create_dispatcher,
                set_webhook,
            )

            # Create bot
            main_bot = create_bot()

            # Create dispatcher and register all normal bot routers
            main_dispatcher = await create_dispatcher()

            # Store them on FastAPI application state so the webhook
            # endpoint can reuse the same objects for every update.
            app.state.telegram_bot = main_bot
            app.state.telegram_dispatcher = main_dispatcher

            # Register Telegram webhook
            if settings.bot_webhook_url:
                await set_webhook(main_bot)

                logger.info(
                    "main_bot_started",
                    webhook_url=settings.bot_webhook_url,
                )
            else:
                logger.warning(
                    "main_bot_webhook_url_missing",
                    message=(
                        "BOT_WEBHOOK_URL is empty. "
                        "Main Telegram bot was created but webhook was not registered."
                    ),
                )

        except Exception as exc:
            # Do not silently kill the whole FastAPI application because
            # Telegram configuration is temporarily unavailable.
            logger.exception(
                "main_bot_startup_failed",
                error=str(exc),
            )

            app.state.telegram_bot = None
            app.state.telegram_dispatcher = None

    else:
        logger.warning(
            "main_bot_token_missing",
            message=(
                "BOT_TOKEN is empty. "
                "Main Telegram bot will not be started."
            ),
        )

        app.state.telegram_bot = None
        app.state.telegram_dispatcher = None

    # ------------------------------------------------------------------------
    # Multi-tenant Telegram bots
    # ------------------------------------------------------------------------
    tenant_manager = None

    try:
        from app.tenants.bot_manager import tenant_bot_manager

        tenant_manager = tenant_bot_manager

        # DOMAIN must be the public HTTPS host without a trailing slash.
        #
        # Example:
        # smmfalcron.onrender.com
        #
        # Tenant webhooks become:
        # https://smmfalcron.onrender.com/bots/{slug}/webhook
        if settings.domain and settings.domain != "localhost":
            try:
                tenant_count = await tenant_manager.startup(
                    AsyncSessionLocal,
                    settings.domain,
                )

                logger.info(
                    "tenant_bot_manager_started",
                    started=tenant_count,
                    domain=settings.domain,
                )

            except Exception as exc:
                logger.exception(
                    "tenant_bot_manager_startup_failed",
                    error=str(exc),
                )
        else:
            logger.warning(
                "tenant_bot_manager_skipped",
                reason="DOMAIN is not configured for public webhook delivery",
            )

    except ImportError:
        logger.warning(
            "tenant_bot_manager_not_available",
        )

    # Store manager even when startup was skipped so routes that reference
    # the singleton continue working.
    app.state.tenant_bot_manager = tenant_manager

    # ------------------------------------------------------------------------
    # Application started
    # ------------------------------------------------------------------------
    logger.info(
        "app_started",
        env=settings.app_env,
    )

    # ------------------------------------------------------------------------
    # Application running
    # ------------------------------------------------------------------------
    try:
        yield

    # ------------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------------
    finally:

        # --------------------------------------------------------------------
        # Stop tenant bots
        # --------------------------------------------------------------------
        try:
            if tenant_manager is not None:
                await tenant_manager.shutdown(
                    delete_webhooks=False,
                )

                logger.info(
                    "tenant_bot_manager_stopped",
                )

        except Exception as exc:
            logger.exception(
                "tenant_bot_manager_shutdown_failed",
                error=str(exc),
            )

        # --------------------------------------------------------------------
        # Stop main Telegram bot
        # --------------------------------------------------------------------
        if main_bot is not None:

            try:
                from app.bot.application import delete_webhook

                # Delete the webhook before closing the Telegram HTTP session.
                await delete_webhook(main_bot)

            except Exception as exc:
                logger.warning(
                    "main_bot_webhook_delete_failed",
                    error=str(exc),
                )

            try:
                await main_bot.session.close()

                logger.info(
                    "main_bot_stopped",
                )

            except Exception as exc:
                logger.warning(
                    "main_bot_shutdown_failed",
                    error=str(exc),
                )

        # --------------------------------------------------------------------
        # Close provider registry and Redis
        # --------------------------------------------------------------------
        try:
            from app.core.redis import close_redis

            await registry.shutdown()
            await close_redis()

        except Exception as exc:
            logger.exception(
                "core_services_shutdown_failed",
                error=str(exc),
            )

        logger.info(
            "app_stopped",
        )


# ============================================================================
# APPLICATION FACTORY
# ============================================================================

def create_app() -> FastAPI:

    app = FastAPI(
        title="SMM Platform API",
        version="1.0.0",
        docs_url="/docs" if settings.app_env != "production" else None,
        redoc_url=None,
        lifespan=lifespan,
    )

    # =========================================================================
    # CORS
    # =========================================================================

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "https://web.telegram.org",
        ],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # =========================================================================
    # PROMETHEUS MIDDLEWARE
    # =========================================================================

    from app.monitoring.instrumentation import PrometheusMiddleware

    app.add_middleware(
        PrometheusMiddleware,
    )

    # =========================================================================
    # ROUTERS
    # =========================================================================

    from app.monitoring.health import router as health_router
    from app.miniapp.routes import router as miniapp_router
    from app.monitoring.instrumentation import metrics_endpoint

    # -------------------------------------------------------------------------
    # Health
    # -------------------------------------------------------------------------

    app.include_router(
        health_router,
    )

    # -------------------------------------------------------------------------
    # Telegram Mini App API
    # -------------------------------------------------------------------------

    app.include_router(
        miniapp_router,
    )

    # -------------------------------------------------------------------------
    # Admin routes
    # -------------------------------------------------------------------------

    try:
        from app.admin.routes import router as admin_router

        app.include_router(
            admin_router,
            prefix="/admin",
        )

    except ImportError:
        logger.warning(
            "admin_routes_not_available",
        )

    # -------------------------------------------------------------------------
    # Own SMM API
    # -------------------------------------------------------------------------

    try:
        from app.api.routes import router as api_router

        app.include_router(
            api_router,
            prefix="/v2",
        )

    except ImportError:
        logger.warning(
            "api_routes_not_available",
        )

    # -------------------------------------------------------------------------
    # Tenant Telegram webhook receiver
    #
    # Existing endpoint:
    #
    # POST /bots/{slug}/webhook
    #
    # The actual implementation remains in app.tenants.routes.
    # -------------------------------------------------------------------------

    try:
        from app.tenants.routes import bot_webhook

        app.include_router(
            bot_webhook,
        )

        logger.info(
            "tenant_webhook_router_registered",
        )

    except ImportError:
        logger.warning(
            "tenant_webhook_router_not_available",
        )

    # =========================================================================
    # PROMETHEUS METRICS
    # =========================================================================

    app.add_api_route(
        "/metrics",
        metrics_endpoint,
        methods=["GET"],
    )

    # =========================================================================
    # MAIN TELEGRAM BOT WEBHOOK
    # =========================================================================

    @app.post("/bot/webhook")
    async def bot_webhook_handler(
        request: Request,
    ):
        """
        Receive Telegram updates for the main SMM bot.

        Telegram sends:
            X-Telegram-Bot-Api-Secret-Token

        The token is checked against BOT_WEBHOOK_SECRET before the
        update is passed to Aiogram.
        """

        # ---------------------------------------------------------------------
        # Get running bot + dispatcher
        # ---------------------------------------------------------------------

        bot = getattr(
            request.app.state,
            "telegram_bot",
            None,
        )

        dispatcher = getattr(
            request.app.state,
            "telegram_dispatcher",
            None,
        )

        if bot is None or dispatcher is None:
            logger.error(
                "main_bot_not_initialized",
            )

            # Return 503 rather than pretending the update was processed.
            return Response(
                content='{"ok":false,"error":"bot_not_initialized"}',
                status_code=503,
                media_type="application/json",
            )

        # ---------------------------------------------------------------------
        # Verify Telegram webhook secret
        # ---------------------------------------------------------------------

        received_secret = request.headers.get(
            "X-Telegram-Bot-Api-Secret-Token",
            "",
        )

        expected_secret = settings.bot_webhook_secret or ""

        if expected_secret:

            if not hmac.compare_digest(
                received_secret,
                expected_secret,
            ):
                logger.warning(
                    "main_bot_webhook_bad_secret",
                )

                return Response(
                    content='{"ok":false}',
                    status_code=403,
                    media_type="application/json",
                )

        else:
            # Production should always have a secret configured.
            logger.warning(
                "main_bot_webhook_secret_missing",
            )

        # ---------------------------------------------------------------------
        # Read Telegram update
        # ---------------------------------------------------------------------

        try:
            update_data = await request.json()

        except Exception as exc:
            logger.warning(
                "main_bot_webhook_invalid_json",
                error=str(exc),
            )

            return Response(
                content='{"ok":false,"error":"invalid_json"}',
                status_code=400,
                media_type="application/json",
            )

        # ---------------------------------------------------------------------
        # Convert JSON → Aiogram Update
        # ---------------------------------------------------------------------

        try:
            from aiogram.types import Update

            update = Update.model_validate(
                update_data,
            )

        except Exception as exc:
            logger.exception(
                "main_bot_webhook_update_validation_failed",
                error=str(exc),
            )

            return Response(
                content='{"ok":false,"error":"invalid_update"}',
                status_code=400,
                media_type="application/json",
            )

        # ---------------------------------------------------------------------
        # Feed update into Aiogram dispatcher
        #
        # This is the part that was missing in the original main.py.
        # Without this, Telegram could reach /bot/webhook but /start would
        # never reach CommandStart().
        # ---------------------------------------------------------------------

        try:
            await dispatcher.feed_update(
                bot,
                update,
            )

            logger.debug(
                "main_bot_update_processed",
                update_id=update.update_id,
            )

        except Exception as exc:
            logger.exception(
                "main_bot_update_processing_failed",
                update_id=update.update_id,
                error=str(exc),
            )

            # Returning 500 tells Telegram the update was not successfully
            # processed and allows Telegram to retry it.
            return Response(
                content='{"ok":false,"error":"processing_failed"}',
                status_code=500,
                media_type="application/json",
            )

        # ---------------------------------------------------------------------
        # Telegram expects a successful HTTP response.
        # ---------------------------------------------------------------------

        return Response(
            content='{"ok":true}',
            status_code=200,
            media_type="application/json",
        )

    # =========================================================================
    # PAYMENT WEBHOOK — RAZORPAY
    # =========================================================================

    @app.post("/webhooks/razorpay")
    async def razorpay_webhook(
        request: Request,
    ):
        from app.core.database import AsyncSessionLocal
        from app.payments.adapters import process_payment_webhook

        body = await request.body()
        headers = dict(request.headers)

        async with AsyncSessionLocal() as db:

            try:
                await process_payment_webhook(
                    db,
                    "razorpay",
                    body,
                    headers,
                )

                await db.commit()

            except Exception as exc:

                logger.error(
                    "razorpay_webhook_error",
                    error=str(exc),
                )

        return {
            "ok": True,
        }

    # =========================================================================
    # PAYMENT WEBHOOK — STRIPE
    # =========================================================================

    @app.post("/webhooks/stripe")
    async def stripe_webhook(
        request: Request,
    ):
        from app.core.database import AsyncSessionLocal
        from app.payments.stripe_adapter import process_stripe_webhook

        body = await request.body()
        headers = dict(request.headers)

        async with AsyncSessionLocal() as db:

            try:
                await process_stripe_webhook(
                    db,
                    body,
                    headers,
                )

                await db.commit()

            except Exception as exc:

                logger.error(
                    "stripe_webhook_error",
                    error=str(exc),
                )

        return {
            "ok": True,
        }

    # =========================================================================
    # REACT / VITE MINI APP
    # =========================================================================

    # Vite builds the frontend into:
    #
    # app/static/miniapp
    #
    # The Dockerfile copies the compiled frontend into this directory.
    #
    # We intentionally mount it after all API routes so that:
    #
    # /health
    # /ready
    # /metrics
    # /miniapp/api/...
    # /bot/webhook
    # /bots/...
    # /admin/...
    # /v2/...
    #
    # continue to be handled by FastAPI.

    static_dir = os.path.join(
        os.path.dirname(__file__),
        "static",
        "miniapp",
    )

    if os.path.isdir(static_dir):

        app.mount(
            "/static/miniapp",
            StaticFiles(
                directory=static_dir,
                html=True,
            ),
            name="miniapp",
        )

        logger.info(
            "miniapp_static_mounted",
            directory=static_dir,
        )

    else:

        logger.warning(
            "miniapp_static_directory_missing",
            directory=static_dir,
        )

    # =========================================================================
    # RETURN APPLICATION
    # =========================================================================

    return app


# ============================================================================
# APPLICATION INSTANCE
# ============================================================================

app = create_app()
