"""
Provider sync engine — fetches services from provider APIs and upserts
into the provider_services table.

Per-provider sync:
  1. Call provider adapter get_services()
  2. For each service: normalize → detect_changes vs stored raw
  3. Insert new services, update changed services, deactivate removed ones
  4. Build SyncResult with counts and diffs for audit

Never overwrites admin-set display_name — only raw_name is synced.
Admin-customised fields (custom_price, profit_pct) are untouched.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.models import Provider, ProviderService as OrmProviderService
from app.sync.normalizer import NormalizedService, ServiceDiff, detect_changes, normalize_service

logger = get_logger(__name__)


@dataclass
class SyncResult:
    provider_id: int
    inserted:    int = 0
    updated:     int = 0
    deactivated: int = 0
    unchanged:   int = 0
    diffs:       list[ServiceDiff] = field(default_factory=list)
    errors:      list[str]         = field(default_factory=list)
    synced_at:   datetime          = field(default_factory=lambda: datetime.now(timezone.utc))


class ProviderSyncEngine:
    def __init__(self, registry) -> None:
        self._registry = registry

    async def sync_provider(
        self, db: AsyncSession, provider_id: int
    ) -> SyncResult:
        result = SyncResult(provider_id=provider_id)

        provider = await db.get(Provider, provider_id)
        if provider is None or not provider.is_active:
            result.errors.append(f"Provider {provider_id} not found or inactive")
            return result

        # Fetch from provider API
        try:
            raw_services = await self._registry.get_services(provider_id)
        except Exception as exc:
            result.errors.append(f"Fetch failed: {exc}")
            return result

        # Load existing stored services
        existing_map: dict[str, OrmProviderService] = {}
        rows = list((await db.execute(
            select(OrmProviderService).where(
                OrmProviderService.provider_id == provider_id
            )
        )).scalars().all())
        for row in rows:
            existing_map[row.provider_svc_id] = row

        seen_ids: set[str] = set()

        for raw in raw_services:
            try:
                norm = normalize_service(raw)
                svc_id = norm.provider_svc_id
                seen_ids.add(svc_id)

                stored = existing_map.get(svc_id)
                diffs  = detect_changes(stored.raw if stored else None, norm)

                if stored is None:
                    # Insert new
                    new_row = OrmProviderService(
                        provider_id=provider_id,
                        provider_svc_id=svc_id,
                        raw_name=norm.raw_name,
                        category=norm.category,
                        rate=norm.rate,
                        min_qty=norm.min_qty,
                        max_qty=norm.max_qty,
                        refill=norm.refill,
                        cancel=norm.cancel,
                        drip_feed=norm.drip_feed,
                        is_active=True,
                        last_seen_at=datetime.now(timezone.utc),
                        raw=norm.raw,
                    )
                    db.add(new_row)
                    result.inserted += 1

                elif diffs:
                    # Update changed fields
                    for diff in diffs:
                        if diff.field == "name":
                            stored.raw_name = diff.new_value
                        elif diff.field == "rate":
                            stored.rate = diff.new_value
                        elif diff.field == "min_qty":
                            stored.min_qty = diff.new_value
                        elif diff.field == "max_qty":
                            stored.max_qty = diff.new_value
                        elif diff.field == "refill":
                            stored.refill = diff.new_value
                        elif diff.field == "cancel":
                            stored.cancel = diff.new_value
                        elif diff.field == "drip_feed":
                            stored.drip_feed = diff.new_value
                    stored.last_seen_at = datetime.now(timezone.utc)
                    stored.raw          = norm.raw
                    result.updated     += 1
                    result.diffs.extend(diffs)

                else:
                    stored.last_seen_at = datetime.now(timezone.utc)
                    result.unchanged   += 1

            except Exception as exc:
                result.errors.append(f"Service error: {exc}")

        # Deactivate services no longer in provider response
        for svc_id, row in existing_map.items():
            if svc_id not in seen_ids and row.is_active:
                row.is_active = False
                result.deactivated += 1

        # Update provider last_sync_at
        provider.last_sync_at = datetime.now(timezone.utc)
        await db.flush()

        logger.info(
            "provider_sync_complete",
            provider_id=provider_id,
            inserted=result.inserted,
            updated=result.updated,
            deactivated=result.deactivated,
            diffs=len(result.diffs),
            errors=len(result.errors),
        )
        return result

    async def sync_all_providers(
        self, db: AsyncSession
    ) -> list[SyncResult]:
        providers = list((await db.execute(
            select(Provider).where(Provider.is_active == True)
        )).scalars().all())

        results = []
        for provider in providers:
            result = await self.sync_provider(db, provider.id)
            results.append(result)
        return results


def build_change_report(results: list[SyncResult]) -> dict[str, Any]:
    return {
        "providers":       len(results),
        "total_inserted":  sum(r.inserted    for r in results),
        "total_updated":   sum(r.updated     for r in results),
        "total_deactivated": sum(r.deactivated for r in results),
        "total_diffs":     sum(len(r.diffs)  for r in results),
        "errors":          [e for r in results for e in r.errors],
    }
