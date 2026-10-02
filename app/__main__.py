"""python -m app  — start API + Telegram bot (polling) + background workers."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

import uvicorn

from app.config import configure_logging, get_settings
from app.db import dispose_engine, init_db, session_scope
from app.seed import seed_if_empty
from app.workers import (
    catalog_sync_loop,
    health_loop,
    order_sync_loop,
    payment_reconcile_loop,
    stuck_orders_loop,
)

log = logging.getLogger("falaron")


def enforce_production_config() -> None:
    """Refuse to boot a production instance that is not safe. Fail loudly, at startup."""
    settings = get_settings()
    for warning in settings.production_warnings():
        log.warning("config: %s", warning)
    problems = settings.production_problems()
    if problems:
        sys.stderr.write("\nRefusing to start: APP_ENV=production but the configuration is unsafe:\n")
        for problem in problems:
            sys.stderr.write(f"  - {problem}\n")
        sys.stderr.write("\nFix .env (see .env.example and DEPLOY.md) and start again.\n")
        raise SystemExit(3)


async def _run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    enforce_production_config()
    log.info(
        "starting FALARON env=%s db=%s gateway=%s polling=%s",
        settings.app_env,
        "sqlite" if settings.is_sqlite else "postgres",
        settings.payment_gateway,
        settings.bot_polling,
    )

    await init_db(settings)
    async with session_scope() as session:
        await seed_if_empty(session, settings)

    from app import notify
    from app.api.main import api_app
    from app.bot.main import create_bot, create_dispatcher

    bot = create_bot(settings)
    dp = create_dispatcher(settings)
    notify.register(bot, settings.owner_telegram_id)

    uv_config = uvicorn.Config(
        api_app,
        host=settings.api_host,
        port=settings.api_port,
        log_level=settings.log_level.lower(),
        loop="asyncio",
        lifespan="on",
        proxy_headers=settings.trust_proxy,
        forwarded_allow_ips="*" if settings.trust_proxy else None,
        server_header=False,
    )
    server = uvicorn.Server(uv_config)
    server.install_signal_handlers = lambda: None  # we own signals so the whole process stops together

    stop = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows
            pass

    async def _api() -> None:
        await server.serve()

    async def _bot() -> None:
        if not settings.bot_polling:
            log.warning("BOT_POLLING=false — bot not started.")
            await stop.wait()
            return
        try:
            await bot.delete_webhook(drop_pending_updates=False)
        except Exception as exc:  # noqa: BLE001
            log.warning("delete_webhook: %s", exc)
        log.info("bot polling started")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types(), handle_signals=False)

    tasks = [
        asyncio.create_task(_api(), name="api"),
        asyncio.create_task(_bot(), name="bot"),
        asyncio.create_task(order_sync_loop(stop), name="order-sync"),
        asyncio.create_task(health_loop(stop), name="health"),
        asyncio.create_task(stuck_orders_loop(stop), name="stuck-orders"),
        asyncio.create_task(catalog_sync_loop(stop), name="catalog-sync"),
        asyncio.create_task(payment_reconcile_loop(stop), name="payment-reconcile"),
    ]

    async def _watch() -> None:
        """First of: stop signal, or a core task dying (api/bot) -> bring everything down."""
        stopper = asyncio.create_task(stop.wait())
        core = [t for t in tasks if t.get_name() in {"api", "bot"}]
        done, _ = await asyncio.wait([stopper, *core], return_when=asyncio.FIRST_COMPLETED)
        for t in done:
            if t is not stopper and t.exception():
                log.error("core task %s crashed: %r", t.get_name(), t.exception())
        stop.set()
        server.should_exit = True
        try:
            await dp.stop_polling()
        except Exception:  # noqa: BLE001
            pass
        stopper.cancel()

    watcher = asyncio.create_task(_watch(), name="watch")
    try:
        await watcher
        await asyncio.wait(tasks, timeout=20)
    finally:
        stop.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await bot.session.close()
        await dispose_engine()
        log.info("shutdown complete")


def main() -> None:
    try:
        get_settings()
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(
            "Missing or invalid configuration. Copy .env.example to .env and set at least BOT_TOKEN "
            f"and OWNER_TELEGRAM_ID.\n({exc})\n"
        )
        raise SystemExit(2) from exc
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
