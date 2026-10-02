"""Background loops: order status sync, provider health, catalog sync, payment reconciliation.

Every loop swallows its own errors (logged) so one bad provider or gateway hiccup can never take
the bot or API down, and every loop exits promptly when `stop` is set.
"""

from __future__ import annotations

import asyncio
import logging
from html import escape

from sqlalchemy import select

from app.catalog import adapter_for
from app.config import get_settings
from app.db import session_scope
from app.models import Provider
from app.notify import notify_admins, notify_user
from app.orders import flag_stuck_orders, list_open_orders, status_label, sync_order_status
from app.pricing import paise_to_rupees_str
from app.security import utcnow

log = logging.getLogger("falaron.workers")

STATUS_SYNC_INTERVAL = 20
HEALTH_INTERVAL = 90
STUCK_CHECK_INTERVAL = 120

_NOTIFY_STATUSES = {"completed", "partial", "canceled", "failed", "refunded"}


async def sync_open_orders_once() -> int:
    updated = 0
    notices: list[tuple[int, str, str]] = []
    async with session_scope() as session:
        orders = await list_open_orders(session, limit=80)
        for order in orders:
            before = order.status
            try:
                await sync_order_status(session, order)
                if order.status != before:
                    updated += 1
                    if order.status in _NOTIFY_STATUSES:
                        notices.append((order.user_id, order.public_id, order.status))
            except Exception:  # noqa: BLE001
                log.exception("status sync failed for %s", order.public_id)
    # After commit, so a user is never told about a change that was rolled back.
    for user_id, public_id, status in notices:
        await notify_user(
            user_id, f"Order <b>{escape(public_id)}</b> is now <b>{escape(status_label(status))}</b>."
        )
    return updated


async def flag_stuck_orders_once() -> int:
    async with session_scope() as session:
        stuck = await flag_stuck_orders(session)
        ids = [o.public_id for o in stuck]
    if ids:
        await notify_admins(
            "⚠️ Orders held for review (no provider outcome recorded): "
            + ", ".join(f"<code>{escape(i)}</code>" for i in ids[:10])
            + "\nFunds are held. Resolve with <code>python -m app.cli resolve-order ID --release</code> "
            "or <code>--attach PROVIDER_ORDER_ID</code>."
        )
    return len(ids)


async def health_check_providers_once() -> None:
    async with session_scope() as session:
        providers = list((await session.execute(select(Provider))).scalars().all())
        for provider in providers:
            try:
                adapter = await adapter_for(session, provider.id)
                result = await adapter.health_check()
                provider.health_status = "ok" if result.ok else "down"
                provider.last_health_detail = (result.detail or "")[:255]
                if result.balance is not None:
                    extra = f" bal={result.balance:.2f}"
                    if len((provider.last_health_detail or "") + extra) < 250:
                        provider.last_health_detail = (provider.last_health_detail or "") + extra
            except Exception as exc:  # noqa: BLE001
                provider.health_status = "down"
                provider.last_health_detail = str(exc)[:255]
                log.warning("health check failed provider=%s", provider.id)
            provider.last_health_at = utcnow()


async def sync_catalog_once() -> str | None:
    """Pull provider catalogs; returns the operator digest (also sent to the owner)."""
    from app.sync import render_alert, sync_all

    async with session_scope() as session:
        reports = await sync_all(session)
    digest = render_alert(reports)
    if digest:
        await notify_admins(digest)
    return digest


async def reconcile_payments_once() -> int:
    from app.payments.service import get_gateway, reconcile_pending

    settings = get_settings()
    gateway = get_gateway(settings)
    if gateway is None:
        return 0
    credited: list[tuple[int, int, str]] = []
    async with session_scope() as session:
        results = await reconcile_pending(session, gateway)
    for r in results:
        if r.outcome == "credited" and r.user_id is not None:
            credited.append((r.user_id, r.amount_paise or 0, r.payment_public_id or ""))
        elif r.outcome == "amount_mismatch":
            await notify_admins(
                f"⚠️ Payment <code>{escape(r.payment_public_id or '?')}</code> amount/currency mismatch — "
                "held for manual review."
            )
    for user_id, amount, pid in credited:
        await notify_user(
            user_id, f"Payment received: <b>{paise_to_rupees_str(amount)}</b> added to your wallet ({escape(pid)})."
        )
    return len(results)


async def _loop(stop: asyncio.Event, name: str, interval: float, fn) -> None:
    log.info("%s loop started (every %ss)", name, interval)
    while not stop.is_set():
        try:
            result = await fn()
            if result:
                log.info("%s: %s", name, result if isinstance(result, (int, str)) else "done")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("%s loop iteration failed", name)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue


async def order_sync_loop(stop: asyncio.Event) -> None:
    await _loop(stop, "order-sync", STATUS_SYNC_INTERVAL, sync_open_orders_once)


async def health_loop(stop: asyncio.Event) -> None:
    await _loop(stop, "provider-health", HEALTH_INTERVAL, health_check_providers_once)


async def stuck_orders_loop(stop: asyncio.Event) -> None:
    await _loop(stop, "stuck-orders", STUCK_CHECK_INTERVAL, flag_stuck_orders_once)


async def catalog_sync_loop(stop: asyncio.Event) -> None:
    interval = max(300, int(get_settings().provider_sync_interval_seconds))
    # Small initial delay so boot-time work (seed, webhook, bot start) finishes first.
    try:
        await asyncio.wait_for(stop.wait(), timeout=30)
        return
    except asyncio.TimeoutError:
        pass
    await _loop(stop, "catalog-sync", interval, sync_catalog_once)


async def payment_reconcile_loop(stop: asyncio.Event) -> None:
    interval = max(60, int(get_settings().payment_reconcile_interval_seconds))
    await _loop(stop, "payment-reconcile", interval, reconcile_payments_once)
