"""Service catalog — CRUD and provider mapping operations."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ServiceNotFoundError, ValidationError
from app.core.logging import get_logger
from app.core.models import Service, ServiceProviderMapping

logger = get_logger(__name__)


def _make_public_id() -> str:
    """Generate SVC-NNNN format public ID."""
    n = int(uuid.uuid4().int % 9000) + 1000
    return f"SVC-{n:04d}"


async def get_service_by_public_id(
    db: AsyncSession, public_id: str
) -> Service:
    svc = (await db.execute(
        select(Service).where(Service.public_id == public_id.upper())
    )).scalar_one_or_none()
    if svc is None:
        raise ServiceNotFoundError(detail=f"Service {public_id!r} not found")
    return svc


async def list_active_services(
    db: AsyncSession,
    category_id: Optional[int] = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[Service], int]:
    stmt = select(Service).where(
        Service.is_active == True,
        Service.ordering_enabled == True,
    )
    if category_id:
        stmt = stmt.where(Service.category_id == category_id)

    total = (await db.execute(
        select(func.count()).select_from(stmt.subquery())
    )).scalar_one()

    items = list((await db.execute(
        stmt.order_by(Service.sort_order, Service.id)
        .offset((page - 1) * page_size).limit(page_size)
    )).scalars().all())

    return items, total


async def create_service(
    db: AsyncSession,
    display_name: str,
    category_id: int,
    custom_price: Optional[Decimal] = None,
    profit_pct: Optional[Decimal] = None,
    refill_enabled: bool = False,
    cancel_enabled: bool = False,
    requires_premium: bool = False,
    sort_order: int = 0,
    actor_id: Optional[int] = None,
) -> Service:
    if not display_name.strip():
        raise ValidationError(detail="display_name cannot be empty")

    now = datetime.now(timezone.utc)
    svc = Service(
        public_id=_make_public_id(),
        display_name=display_name.strip(),
        category_id=category_id,
        custom_price=custom_price,
        profit_pct=profit_pct,
        is_active=True,
        ordering_enabled=True,
        refill_enabled=refill_enabled,
        cancel_enabled=cancel_enabled,
        requires_premium=requires_premium,
        sort_order=sort_order,
        created_at=now,
        updated_at=now,
    )
    db.add(svc)
    await db.flush()
    logger.info("service_created", service_id=svc.id, public_id=svc.public_id, actor_id=actor_id)
    return svc


async def update_service(
    db: AsyncSession,
    service_id: int,
    updates: dict,
    actor_id: Optional[int] = None,
) -> Service:
    _ALLOWED = {"display_name","custom_price","profit_pct","is_active",
                "ordering_enabled","refill_enabled","cancel_enabled",
                "requires_premium","sort_order"}
    invalid = set(updates) - _ALLOWED
    if invalid:
        raise ValidationError(detail=f"Cannot update fields: {invalid}")

    svc = await db.get(Service, service_id)
    if svc is None:
        raise ServiceNotFoundError(detail=f"Service {service_id} not found")

    for k, v in updates.items():
        setattr(svc, k, v)
    svc.updated_at = datetime.now(timezone.utc)
    await db.flush()
    logger.info("service_updated", service_id=service_id, fields=list(updates), actor_id=actor_id)
    return svc


async def map_provider_service(
    db: AsyncSession,
    service_id: int,
    provider_id: int,
    provider_svc_id: str,
    is_primary: bool = True,
    routing_weight: int = 100,
) -> ServiceProviderMapping:
    mapping = ServiceProviderMapping(
        service_id=service_id,
        provider_id=provider_id,
        provider_svc_id=provider_svc_id,
        is_primary=is_primary,
        routing_weight=routing_weight,
    )
    db.add(mapping)
    await db.flush()
    return mapping


async def resolve_provider_mapping(
    db: AsyncSession, service_id: int
) -> Optional[ServiceProviderMapping]:
    return (await db.execute(
        select(ServiceProviderMapping).where(
            ServiceProviderMapping.service_id == service_id,
            ServiceProviderMapping.is_primary == True,
        )
    )).scalar_one_or_none()
