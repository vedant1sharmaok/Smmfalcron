"""Catalog queries and kill-switch helpers."""

from __future__ import annotations

import time
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.adapters.base import ProviderAdapter, ProviderError
from app.adapters.mock import MockAdapter
from app.adapters.perfectpanel import PerfectPanelAdapter
from app.config import Settings, get_settings
from app.models import (
    KILL_MAINTENANCE,
    KILL_NEW_ORDERS,
    KILL_PAYMENTS,
    KILL_READ_ONLY,
    KILL_REFILLS,
    AppSetting,
    Category,
    Provider,
    Service,
    StarPrice,
)
from app.net import UnsafeUrl, validate_provider_url
from app.security import decrypt_secret

# provider id -> (adapter, built_at). Short TTL so a key rotated from the CLI (another process)
# is picked up by the running bot without a restart.
_ADAPTER_TTL_SECONDS = 60.0
_ADAPTERS: dict[str, tuple[ProviderAdapter, float]] = {}


async def get_setting(session: AsyncSession, key: str, default: str = "") -> str:
    row = await session.get(AppSetting, key)
    return row.value if row is not None else default


async def set_setting(session: AsyncSession, key: str, value: str) -> AppSetting:
    from app.security import utcnow

    row = await session.get(AppSetting, key)
    if row is None:
        row = AppSetting(key=key, value=value, updated_at=utcnow())
        session.add(row)
    else:
        row.value = value
        row.updated_at = utcnow()
    await session.flush()
    return row


async def is_kill_on(session: AsyncSession, key: str) -> bool:
    return (await get_setting(session, key, "0")).strip() in {"1", "true", "True", "yes"}


async def kill_switches(session: AsyncSession) -> dict[str, bool]:
    keys = [KILL_NEW_ORDERS, KILL_PAYMENTS, KILL_REFILLS, KILL_READ_ONLY, KILL_MAINTENANCE]
    return {key: await is_kill_on(session, key) for key in keys}


async def assert_orders_open(session: AsyncSession) -> None:
    flags = await kill_switches(session)
    if flags[KILL_MAINTENANCE]:
        raise PermissionError("FALARON is in maintenance. Please try again later.")
    if flags[KILL_READ_ONLY]:
        raise PermissionError("The platform is in read-only mode.")
    if flags[KILL_NEW_ORDERS]:
        raise PermissionError("New orders are temporarily paused.")


async def assert_payments_open(session: AsyncSession) -> None:
    flags = await kill_switches(session)
    if flags[KILL_MAINTENANCE] or flags[KILL_READ_ONLY] or flags[KILL_PAYMENTS]:
        raise PermissionError("Deposits are temporarily paused.")


async def assert_refills_open(session: AsyncSession) -> None:
    flags = await kill_switches(session)
    if flags[KILL_MAINTENANCE] or flags[KILL_READ_ONLY] or flags[KILL_REFILLS]:
        raise PermissionError("Refills are temporarily paused.")


async def list_categories(session: AsyncSession, *, active_only: bool = True) -> list[Category]:
    stmt = select(Category).order_by(Category.sort_order, Category.name)
    if active_only:
        stmt = stmt.where(Category.is_active.is_(True))
    return list((await session.execute(stmt)).scalars().all())


async def list_services(
    session: AsyncSession,
    category_id: str,
    *,
    active_only: bool = True,
) -> list[Service]:
    stmt = (
        select(Service)
        .options(selectinload(Service.provider), selectinload(Service.stars))
        .where(Service.category_id == category_id)
        .order_by(Service.name)
    )
    if active_only:
        stmt = stmt.where(Service.is_active.is_(True))
    return list((await session.execute(stmt)).scalars().all())


async def get_service(session: AsyncSession, service_id: str) -> Service | None:
    result = await session.execute(
        select(Service)
        .options(
            selectinload(Service.category),
            selectinload(Service.provider),
            selectinload(Service.stars),
        )
        .where(Service.id == service_id)
    )
    return result.scalar_one_or_none()


async def featured_stars(session: AsyncSession) -> list[tuple[StarPrice, Service]]:
    result = await session.execute(
        select(StarPrice, Service)
        .join(Service, Service.id == StarPrice.service_id)
        .where(StarPrice.is_active.is_(True), StarPrice.user_id.is_(None), Service.is_active.is_(True))
    )
    return list(result.all())


async def count_active_services(session: AsyncSession) -> int:
    result = await session.execute(
        select(func.count()).select_from(Service).where(Service.is_active.is_(True))
    )
    return int(result.scalar_one())


def build_adapter(provider: Provider, settings: Settings | None = None) -> ProviderAdapter:
    settings = settings or get_settings()
    cached = _ADAPTERS.get(provider.id)
    if cached is not None and (time.monotonic() - cached[1]) < _ADAPTER_TTL_SECONDS:
        return cached[0]

    adapter_type = (provider.adapter_type or "mock").lower()
    adapter: ProviderAdapter
    if adapter_type == "perfectpanel":
        api_key = ""
        if provider.encrypted_api_key:
            try:
                api_key = decrypt_secret(provider.encrypted_api_key, settings)
            except ValueError:
                api_key = ""
        base_url = provider.base_url or ""
        if not base_url or not api_key:
            if settings.is_production:
                raise ProviderError(f"Provider {provider.id} is not configured")
            adapter = MockAdapter(provider.id)  # development convenience only
        else:
            try:
                validate_provider_url(
                    base_url,
                    allow_private=settings.allow_private_provider_urls or not settings.is_production,
                    require_https=settings.is_production,
                )
            except UnsafeUrl as exc:
                raise ProviderError(f"Provider {provider.id} URL rejected: {exc}") from exc
            adapter = PerfectPanelAdapter(
                provider.id,
                base_url,
                api_key,
                timeout=float(provider.timeout_seconds or 30),
            )
    else:
        if settings.is_production:
            # A mock provider would "accept" orders that are never delivered while charging real money.
            raise ProviderError(f"Provider {provider.id} uses the demo adapter, which is disabled in production")
        adapter = MockAdapter(provider.id)
    _ADAPTERS[provider.id] = (adapter, time.monotonic())
    return adapter


def reset_adapter_cache() -> None:
    _ADAPTERS.clear()


async def adapter_for(session: AsyncSession, provider_id: str) -> ProviderAdapter:
    provider = await session.get(Provider, provider_id)
    if provider is None:
        raise ValueError(f"Unknown provider {provider_id}")
    return build_adapter(provider)


def service_card_payload(service: Service) -> dict[str, Any]:
    return {
        "id": service.id,
        "name": service.name,
        "description": service.description,
        "category_id": service.category_id,
        "provider_id": service.provider_id,
        "type": service.service_type,
        "min_qty": service.min_qty,
        "max_qty": service.max_qty,
        "refillable": service.is_refillable,
        "cancelable": service.is_cancelable,
        "average_time": service.average_time,
        "cost_per_1000_paise": service.rate_per_1000_paise,
    }
