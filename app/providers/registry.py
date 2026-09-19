"""
Provider registry — the single source of provider routing truth.

Responsibilities:
  - Load active providers from DB on startup / refresh.
  - Decrypt API credentials (AES-GCM) before passing to adapters.
  - Instantiate and cache adapter instances keyed by provider_id.
  - Maintain circuit-breaker state in Redis (5 failures → degraded,
    10 → down; recovery requires admin action or auto-reset after 30m).
  - Route order/refill/cancel operations to the correct adapter.
  - Health-check all providers and update their DB status.

The registry is a singleton — import `registry` from this module.
It is initialised at application startup (called from lifespan).

NEVER expose raw credentials.  Decryption is done in-memory only;
the plaintext key exists only for the duration of the adapter call.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import decrypt_provider_credential
from app.core.exceptions import ProviderError, ProviderUnavailableError
from app.core.logging import get_logger
from app.core.models import Provider
from app.core.redis import RedisKeys, get_redis
from app.providers.base import BaseProviderAdapter
from app.providers.models import (
    CancelResult,
    HealthResult,
    OrderRequest,
    ProviderHealthStatus,
    ProviderOrderResult,
    ProviderOrderStatus,
    ProviderRefillResult,
    ProviderRefillStatus,
    ProviderService,
)
from app.providers.smmpromax import SMMProMaxAdapter

logger = get_logger(__name__)

# Circuit breaker thresholds.
_CB_DEGRADED_THRESHOLD = 5
_CB_DOWN_THRESHOLD     = 10
_CB_AUTO_RESET_S       = 1800   # 30 minutes — auto-reset down state


@dataclass
class ProviderEntry:
    """In-memory record for one loaded provider."""
    db_id: int
    name: str
    slug: str
    endpoint: str
    currency: str
    priority: int
    adapter: BaseProviderAdapter
    health_status: ProviderHealthStatus = ProviderHealthStatus.HEALTHY
    failure_count: int = 0
    last_failure_at: float = 0.0


class ProviderRegistry:
    """
    Singleton registry — one instance shared across the application.
    Not thread-safe at the Python level, but safe under asyncio's
    single-threaded event loop model.
    """

    def __init__(self) -> None:
        self._providers: dict[int, ProviderEntry] = {}   # keyed by provider_id
        self._lock = asyncio.Lock()

    # ── Startup / refresh ──────────────────────────────────────────────────────

    async def load_providers(self, db: AsyncSession) -> int:
        """
        Load all active providers from the database.
        Decrypts credentials in-memory and instantiates adapters.
        Called once at startup; may be called again to refresh.
        Returns the number of providers loaded.
        """
        async with self._lock:
            stmt = select(Provider).where(
                Provider.is_active == True,
            ).order_by(Provider.priority.desc())

            result = await db.execute(stmt)
            db_providers = result.scalars().all()

            new_providers: dict[int, ProviderEntry] = {}
            for p in db_providers:
                try:
                    api_key = self._decrypt_credential(p)
                    adapter = self._build_adapter(p, api_key)
                    # Preserve health state if already loaded.
                    existing = self._providers.get(p.id)
                    new_providers[p.id] = ProviderEntry(
                        db_id=p.id,
                        name=p.name,
                        slug=p.slug,
                        endpoint=p.endpoint,
                        currency=p.currency,
                        priority=p.priority,
                        adapter=adapter,
                        health_status=(
                            existing.health_status
                            if existing else ProviderHealthStatus.HEALTHY
                        ),
                        failure_count=existing.failure_count if existing else 0,
                    )
                    logger.info("provider_loaded", provider_id=p.id, name=p.name)
                except Exception as exc:
                    logger.error(
                        "provider_load_failed",
                        provider_id=p.id,
                        name=p.name,
                        error=str(exc),
                    )

            # Close HTTP clients for providers no longer active.
            removed = set(self._providers) - set(new_providers)
            for pid in removed:
                try:
                    await self._providers[pid].adapter.close()
                except Exception:
                    pass

            self._providers = new_providers
            logger.info("provider_registry_loaded", count=len(new_providers))
            return len(new_providers)

    def _decrypt_credential(self, provider: Provider) -> str:
        """Decrypt the provider's API key. Raises if credentials_enc is missing."""
        if not provider.credentials_enc:
            raise ProviderError(
                detail=f"Provider {provider.name} has no encrypted credentials"
            )
        return decrypt_provider_credential(provider.credentials_enc)

    def _build_adapter(self, provider: Provider, api_key: str) -> BaseProviderAdapter:
        """
        Instantiate the correct adapter class for this provider.
        Extend this method when adding new adapter types.
        The `slug` field on the Provider row controls which adapter is used.
        """
        # All providers with no specific slug mapping use the standard SMM panel adapter.
        return SMMProMaxAdapter(
            provider_id=provider.id,
            name=provider.name,
            endpoint=provider.endpoint,
            api_key=api_key,
        )

    async def shutdown(self) -> None:
        """Close all HTTP clients. Called from application lifespan shutdown."""
        async with self._lock:
            for entry in self._providers.values():
                try:
                    await entry.adapter.close()
                except Exception:
                    pass
            self._providers.clear()

    # ── Provider lookup ────────────────────────────────────────────────────────

    def get_provider(self, provider_id: int) -> ProviderEntry:
        """Return a loaded provider entry. Raises ProviderUnavailableError if not found."""
        entry = self._providers.get(provider_id)
        if entry is None:
            raise ProviderUnavailableError(
                detail=f"Provider {provider_id} not loaded in registry",
            )
        return entry

    def get_healthy_providers(self) -> list[ProviderEntry]:
        """Return providers that are not DOWN, sorted by priority descending."""
        return sorted(
            [e for e in self._providers.values()
             if e.health_status != ProviderHealthStatus.DOWN],
            key=lambda e: e.priority,
            reverse=True,
        )

    def get_all_providers(self) -> list[ProviderEntry]:
        """Return all loaded providers regardless of health status."""
        return sorted(self._providers.values(), key=lambda e: e.priority, reverse=True)

    # ── Circuit breaker ────────────────────────────────────────────────────────

    async def record_success(self, provider_id: int) -> None:
        """Reset the failure counter on a successful provider call."""
        entry = self._providers.get(provider_id)
        if entry:
            entry.failure_count = 0
            if entry.health_status == ProviderHealthStatus.DEGRADED:
                entry.health_status = ProviderHealthStatus.HEALTHY
                logger.info("provider_circuit_breaker_reset", provider_id=provider_id)

    async def record_failure(self, provider_id: int) -> None:
        """
        Increment failure count and trip the circuit breaker if thresholds are exceeded.
        Updates Redis so the health worker sees the current state.
        """
        entry = self._providers.get(provider_id)
        if not entry:
            return

        entry.failure_count += 1
        entry.last_failure_at = time.time()

        if entry.failure_count >= _CB_DOWN_THRESHOLD:
            if entry.health_status != ProviderHealthStatus.DOWN:
                entry.health_status = ProviderHealthStatus.DOWN
                logger.error(
                    "provider_circuit_breaker_tripped_down",
                    provider_id=provider_id,
                    failures=entry.failure_count,
                )
        elif entry.failure_count >= _CB_DEGRADED_THRESHOLD:
            if entry.health_status == ProviderHealthStatus.HEALTHY:
                entry.health_status = ProviderHealthStatus.DEGRADED
                logger.warning(
                    "provider_circuit_breaker_degraded",
                    provider_id=provider_id,
                    failures=entry.failure_count,
                )

        redis = get_redis()
        await redis.setex(
            RedisKeys.circuit_breaker(provider_id),
            _CB_AUTO_RESET_S,
            f"{entry.failure_count}:{entry.health_status}",
        )

    async def reset_circuit_breaker(self, provider_id: int, actor_id: int) -> None:
        """Manual circuit breaker reset — called by admin action."""
        entry = self._providers.get(provider_id)
        if entry:
            entry.failure_count = 0
            entry.health_status = ProviderHealthStatus.HEALTHY
            redis = get_redis()
            await redis.delete(RedisKeys.circuit_breaker(provider_id))
            logger.info(
                "provider_circuit_breaker_manual_reset",
                provider_id=provider_id,
                actor_id=actor_id,
            )

    # ── Wrapped adapter operations ─────────────────────────────────────────────
    # These wrap every adapter call with circuit-breaker recording.

    async def get_services(self, provider_id: int) -> list[ProviderService]:
        entry = self.get_provider(provider_id)
        try:
            result = await entry.adapter.get_services()
            await self.record_success(provider_id)
            return result
        except (ProviderError, ProviderUnavailableError):
            await self.record_failure(provider_id)
            raise

    async def get_balance(self, provider_id: int) -> Decimal:
        entry = self.get_provider(provider_id)
        try:
            result = await entry.adapter.get_balance()
            await self.record_success(provider_id)
            return result
        except (ProviderError, ProviderUnavailableError):
            await self.record_failure(provider_id)
            raise

    async def create_order(
        self, provider_id: int, req: OrderRequest
    ) -> ProviderOrderResult:
        entry = self.get_provider(provider_id)
        if entry.health_status == ProviderHealthStatus.DOWN:
            raise ProviderUnavailableError(
                detail=f"Provider {provider_id} circuit breaker is open (DOWN)",
            )
        try:
            result = await entry.adapter.create_order(req)
            await self.record_success(provider_id)
            return result
        except (ProviderError, ProviderUnavailableError):
            await self.record_failure(provider_id)
            raise

    async def get_order_status(
        self, provider_id: int, provider_order_id: str
    ) -> ProviderOrderStatus:
        entry = self.get_provider(provider_id)
        try:
            result = await entry.adapter.get_order_status(provider_order_id)
            await self.record_success(provider_id)
            return result
        except (ProviderError, ProviderUnavailableError):
            await self.record_failure(provider_id)
            raise

    async def get_multiple_order_status(
        self, provider_id: int, provider_order_ids: list[str]
    ) -> list[ProviderOrderStatus]:
        entry = self.get_provider(provider_id)
        try:
            result = await entry.adapter.get_multiple_order_status(provider_order_ids)
            await self.record_success(provider_id)
            return result
        except (ProviderError, ProviderUnavailableError):
            await self.record_failure(provider_id)
            raise

    async def refill(
        self, provider_id: int, provider_order_id: str
    ) -> ProviderRefillResult:
        entry = self.get_provider(provider_id)
        try:
            result = await entry.adapter.refill(provider_order_id)
            await self.record_success(provider_id)
            return result
        except (ProviderError, ProviderUnavailableError):
            await self.record_failure(provider_id)
            raise

    async def get_refill_status(
        self, provider_id: int, refill_id: str
    ) -> ProviderRefillStatus:
        entry = self.get_provider(provider_id)
        try:
            result = await entry.adapter.get_refill_status(refill_id)
            await self.record_success(provider_id)
            return result
        except (ProviderError, ProviderUnavailableError):
            await self.record_failure(provider_id)
            raise

    async def get_multiple_refill_status(
        self, provider_id: int, refill_ids: list[str]
    ) -> list[ProviderRefillStatus]:
        entry = self.get_provider(provider_id)
        try:
            result = await entry.adapter.get_multiple_refill_status(refill_ids)
            await self.record_success(provider_id)
            return result
        except (ProviderError, ProviderUnavailableError):
            await self.record_failure(provider_id)
            raise

    async def cancel(
        self, provider_id: int, provider_order_id: str
    ) -> CancelResult:
        entry = self.get_provider(provider_id)
        try:
            result = await entry.adapter.cancel(provider_order_id)
            await self.record_success(provider_id)
            return result
        except (ProviderError, ProviderUnavailableError):
            await self.record_failure(provider_id)
            raise

    async def health_check(self, provider_id: int) -> HealthResult:
        entry = self.get_provider(provider_id)
        result = await entry.adapter.health_check()

        if result.status == ProviderHealthStatus.HEALTHY:
            await self.record_success(provider_id)
        else:
            await self.record_failure(provider_id)

        return result

    async def health_check_all(self) -> dict[int, HealthResult]:
        """Run health checks on all loaded providers concurrently."""
        tasks = {
            pid: asyncio.create_task(self.health_check(pid))
            for pid in self._providers
        }
        results: dict[int, HealthResult] = {}
        for pid, task in tasks.items():
            try:
                results[pid] = await task
            except Exception as exc:
                results[pid] = HealthResult(
                    status=ProviderHealthStatus.DOWN,
                    error=str(exc),
                )
        return results


# ── Module-level singleton ─────────────────────────────────────────────────────
registry = ProviderRegistry()
