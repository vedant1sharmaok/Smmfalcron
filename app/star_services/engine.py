"""
Star Services engine — Section 10 of blueprint.

Star Services are curated customer-facing services backed by one or
more provider mappings. The engine selects the best provider at
order time using the configured routing mode.

Routing modes:
  cheapest   — pick the provider with the lowest rate
  fastest    — pick the provider with the best health/latency
  healthiest — pick the provider with highest success rate
  priority   — use provider weights, highest weight first
  weighted   — weighted random selection by provider weight
  manual     — use the fixed preferred_provider_id

Admin can configure per-Star-Service:
  - Custom display name, description, price, profit %
  - Preferred + fallback providers
  - Routing mode + per-provider weights
  - Featured flag, custom emoji/icon
  - Independent maintenance state
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ServiceUnavailableError, ProviderUnavailableError
from app.core.logging import get_logger

logger = get_logger(__name__)


@dataclass
class StarServiceMapping:
    """Single provider mapping within a Star Service."""
    provider_id:      int
    provider_svc_id:  str
    weight:           int   = 100
    is_preferred:     bool  = False
    is_fallback:      bool  = False


@dataclass
class StarServiceRouteResult:
    """Result of routing a Star Service to a specific provider."""
    provider_id:      int
    provider_svc_id:  str
    rate:             Decimal
    routing_mode:     str
    selected_by:      str  # "preferred" | "weighted" | "cheapest" | "healthiest" | "fallback"


async def route_star_service(
    db: AsyncSession,
    star_service_id: int,
    routing_mode: str = "priority",
) -> StarServiceRouteResult:
    """
    Select the best provider mapping for a Star Service at order time.

    Called by the order engine when placing an order for a Star Service.
    Never exposes provider details to the customer — result is used
    internally by the order engine only.
    """
    from app.core.models import Provider, ProviderService

    # Load mappings (stored as JSON in star_service config or a join table)
    # For now: load from service_provider_mappings filtered by star flag
    from sqlalchemy import select
    from app.core.models import ServiceProviderMapping

    mappings = list((await db.execute(
        select(ServiceProviderMapping).where(
            ServiceProviderMapping.service_id == star_service_id,
        ).order_by(ServiceProviderMapping.routing_weight.desc())
    )).scalars().all())

    if not mappings:
        raise ServiceUnavailableError(
            detail=f"Star service {star_service_id} has no provider mappings",
        )

    # Build candidates with live provider data
    candidates = []
    for m in mappings:
        provider = await db.get(Provider, m.provider_id)
        if not provider or not provider.is_active:
            continue

        prov_svc = (await db.execute(
            select(ProviderService).where(
                ProviderService.provider_id == m.provider_id,
                ProviderService.provider_svc_id == m.provider_svc_id,
            )
        )).scalar_one_or_none()
        if not prov_svc or not prov_svc.is_active:
            continue

        candidates.append({
            "provider_id":     m.provider_id,
            "provider_svc_id": m.provider_svc_id,
            "rate":            prov_svc.rate,
            "weight":          m.routing_weight or 100,
            "is_primary":      m.is_primary,
            "health":          provider.health_status or "unknown",
        })

    if not candidates:
        raise ProviderUnavailableError(
            detail=f"No healthy providers for star service {star_service_id}",
        )

    selected  = None
    selected_by = "priority"

    if routing_mode == "cheapest":
        selected    = min(candidates, key=lambda c: c["rate"])
        selected_by = "cheapest"

    elif routing_mode == "healthiest":
        healthy   = [c for c in candidates if c["health"] == "healthy"]
        selected  = healthy[0] if healthy else candidates[0]
        selected_by = "healthiest"

    elif routing_mode == "weighted":
        weights  = [c["weight"] for c in candidates]
        selected = random.choices(candidates, weights=weights, k=1)[0]
        selected_by = "weighted"

    elif routing_mode == "manual":
        # Use primary/preferred mapping
        primary  = next((c for c in candidates if c["is_primary"]), None)
        selected = primary or candidates[0]
        selected_by = "manual"

    else:  # priority (default) — highest weight first
        selected    = candidates[0]
        selected_by = "priority"

    logger.info(
        "star_service_routed",
        star_service_id=star_service_id,
        routing_mode=routing_mode,
        selected_by=selected_by,
        provider_id=selected["provider_id"],
    )

    return StarServiceRouteResult(
        provider_id=selected["provider_id"],
        provider_svc_id=selected["provider_svc_id"],
        rate=selected["rate"],
        routing_mode=routing_mode,
        selected_by=selected_by,
    )


async def get_featured_star_services(
    db: AsyncSession,
    limit: int = 10,
) -> list[dict]:
    """
    Return featured Star Services for display in the bot/Mini App.
    Never exposes provider_id, routing, or upstream pricing.
    """
    from app.core.models import Service

    featured = list((await db.execute(
        select(Service).where(
            Service.is_active == True,
            Service.ordering_enabled == True,
        ).order_by(Service.sort_order.asc()).limit(limit)
    )).scalars().all())

    results = []
    for svc in featured:
        results.append({
            "public_id":    svc.public_id,
            "display_name": svc.display_name,
            "custom_price": str(svc.custom_price) if svc.custom_price else None,
        })
    return results
