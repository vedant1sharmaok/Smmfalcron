"""
TenantBotManager — per-tenant bot instance lifecycle management.

Manages the lifecycle of multiple aiogram bot instances running concurrently.
Each tenant gets its own Bot + Dispatcher + Router stack, isolated from
all other tenants at the Python object level.

Startup:
  1. Load all active tenants from DB
  2. For each tenant, create Bot(token=...) and wire the standard handler set
  3. Register webhooks for each tenant at /bots/{slug}/webhook
  4. Store running instances in _instances dict keyed by tenant_id

Shutdown:
  1. Gracefully close all bot sessions (httpx clients)
  2. Delete webhooks if DISABLE_WEBHOOK_ON_SHUTDOWN=true
  3. Clear _instances

Health monitoring:
  health_check_all() pings Telegram getMe for each bot — called every 5 min
  by the provider health check worker.

Isolation:
  Each tenant's handlers receive a TenantContext containing the tenant_id
  and tenant-scoped DB session. The order engine and wallet ledger receive
  tenant_id as an explicit parameter — they never infer it from global state.

Webhook URL pattern:
  https://{DOMAIN}/bots/{slug}/webhook
  The slug identifies which tenant's bot received the update.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from app.core.logging import get_logger
from app.tenants.service import get_decrypted_bot_token

logger = get_logger(__name__)


class TenantBotInstance:
    """
    A running bot instance for one tenant.
    Wraps aiogram Bot + Dispatcher with tenant-scoped context.
    """

    def __init__(
        self,
        tenant_id: int,
        tenant_slug: str,
        bot_token: str,
        webhook_secret: str,
    ) -> None:
        self.tenant_id      = tenant_id
        self.tenant_slug    = tenant_slug
        self._bot_token     = bot_token          # decrypted — held in memory only
        self._webhook_secret = webhook_secret
        self.bot            = None
        self.dispatcher     = None
        self.started_at     = None
        self.last_update_at = None
        self.update_count   = 0
        self.is_healthy     = False

    async def start(self, domain: str) -> None:
        """
        Initialise the aiogram Bot and Dispatcher, wire handlers,
        and register the webhook.
        """
        from aiogram import Bot, Dispatcher
        from aiogram.client.default import DefaultBotProperties
        from aiogram.enums import ParseMode
        from app.tenants.handlers import build_tenant_router

        self.bot = Bot(
            token=self._bot_token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        self.dispatcher = Dispatcher()

        # Wire tenant-scoped handler router
        tenant_router = build_tenant_router(self.tenant_id)
        self.dispatcher.include_router(tenant_router)

        # Register webhook with Telegram
        webhook_url = f"https://{domain}/bots/{self.tenant_slug}/webhook"
        await self.bot.set_webhook(
            url=webhook_url,
            secret_token=self._webhook_secret,
            drop_pending_updates=False,
            allowed_updates=["message", "callback_query"],
        )

        self.started_at = datetime.now(timezone.utc)
        self.is_healthy = True

        logger.info(
            "tenant_bot_started",
            tenant_id=self.tenant_id,
            slug=self.tenant_slug,
            webhook_url=webhook_url,
        )

    async def stop(self, delete_webhook: bool = False) -> None:
        """Gracefully stop the bot instance."""
        if self.bot is None:
            return
        try:
            if delete_webhook:
                await self.bot.delete_webhook()
            await self.bot.session.close()
        except Exception as exc:
            logger.warning(
                "tenant_bot_stop_error",
                tenant_id=self.tenant_id,
                error=str(exc),
            )
        finally:
            self.is_healthy = False
            logger.info("tenant_bot_stopped", tenant_id=self.tenant_id)

    async def health_check(self) -> bool:
        """
        Ping Telegram getMe to verify the bot is reachable.
        Updates is_healthy and returns the result.
        """
        if self.bot is None:
            self.is_healthy = False
            return False
        try:
            me = await self.bot.get_me()
            self.is_healthy = me is not None
            return self.is_healthy
        except Exception as exc:
            logger.warning(
                "tenant_bot_health_failed",
                tenant_id=self.tenant_id,
                error=str(exc),
            )
            self.is_healthy = False
            return False

    async def process_update(self, update_data: dict) -> None:
        """Feed a webhook update to this bot's dispatcher."""
        from aiogram.types import Update
        update = Update.model_validate(update_data)
        await self.dispatcher.feed_update(self.bot, update)
        self.last_update_at = datetime.now(timezone.utc)
        self.update_count  += 1

    def __repr__(self) -> str:
        return (
            f"<TenantBotInstance tenant_id={self.tenant_id} "
            f"slug={self.tenant_slug!r} healthy={self.is_healthy}>"
        )


class TenantBotManager:
    """
    Singleton manager for all tenant bot instances.
    Loaded once at application startup; referenced by the webhook route.
    """

    def __init__(self) -> None:
        # keyed by tenant_id
        self._instances: dict[int, TenantBotInstance] = {}
        # keyed by slug (for webhook routing)
        self._by_slug:   dict[str, TenantBotInstance] = {}
        self._domain: str = ""

    async def startup(self, db_session_factory, domain: str) -> int:
        """
        Load all active tenants from DB and start their bot instances.
        Returns the number of bots started.
        """
        from app.core.models import Tenant
        from sqlalchemy import select

        self._domain = domain
        started = 0

        async with db_session_factory() as db:
            tenants = list((await db.execute(
                select(Tenant).where(
                    Tenant.is_active == True,
                    Tenant.is_deleted == False,
                )
            )).scalars().all())

        logger.info("tenant_bot_manager_starting", tenant_count=len(tenants))

        for tenant in tenants:
            try:
                await self._start_instance(tenant, domain)
                started += 1
            except Exception as exc:
                logger.error(
                    "tenant_bot_start_failed",
                    tenant_id=tenant.id,
                    slug=tenant.slug,
                    error=str(exc),
                )

        logger.info("tenant_bot_manager_ready", started=started)
        return started

    async def shutdown(self, delete_webhooks: bool = False) -> None:
        """Stop all running bot instances."""
        logger.info("tenant_bot_manager_shutting_down", count=len(self._instances))
        await asyncio.gather(
            *[inst.stop(delete_webhook=delete_webhooks)
              for inst in self._instances.values()],
            return_exceptions=True,
        )
        self._instances.clear()
        self._by_slug.clear()

    async def start_tenant(self, tenant) -> TenantBotInstance:
        """
        Start a single tenant bot instance.
        Called when a tenant is activated via the admin API.
        """
        if tenant.id in self._instances:
            return self._instances[tenant.id]
        return await self._start_instance(tenant, self._domain)

    async def stop_tenant(self, tenant_id: int, delete_webhook: bool = False) -> None:
        """
        Stop a single tenant bot instance.
        Called when a tenant is suspended or deleted.
        """
        inst = self._instances.pop(tenant_id, None)
        if inst is None:
            return
        self._by_slug.pop(inst.tenant_slug, None)
        await inst.stop(delete_webhook=delete_webhook)

    def get_instance(self, tenant_id: int) -> TenantBotInstance | None:
        return self._instances.get(tenant_id)

    def get_instance_by_slug(self, slug: str) -> TenantBotInstance | None:
        return self._by_slug.get(slug)

    async def health_check_all(self) -> dict[int, bool]:
        """
        Health-check all running bots.
        Returns dict of tenant_id → is_healthy.
        """
        results = {}
        checks  = [
            (tenant_id, inst.health_check())
            for tenant_id, inst in self._instances.items()
        ]
        for tenant_id, coro in checks:
            try:
                results[tenant_id] = await coro
            except Exception:
                results[tenant_id] = False
        return results

    def status_summary(self) -> dict[str, Any]:
        """Return a summary dict for the /health endpoint."""
        return {
            "total":    len(self._instances),
            "healthy":  sum(1 for i in self._instances.values() if i.is_healthy),
            "unhealthy":sum(1 for i in self._instances.values() if not i.is_healthy),
            "slugs":    list(self._by_slug.keys()),
        }

    async def _start_instance(self, tenant, domain: str) -> TenantBotInstance:
        """Decrypt token, create instance, start it, register in both dicts."""
        bot_token = get_decrypted_bot_token(tenant)
        inst = TenantBotInstance(
            tenant_id=tenant.id,
            tenant_slug=tenant.slug,
            bot_token=bot_token,
            webhook_secret=tenant.webhook_secret,
        )
        await inst.start(domain)
        self._instances[tenant.id] = inst
        self._by_slug[tenant.slug] = inst
        return inst


# ── Global singleton ───────────────────────────────────────────────────────────
# Imported by app/main.py and the webhook route handler.
tenant_bot_manager = TenantBotManager()
