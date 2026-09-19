"""
Order monitor worker — Section 14 of blueprint.
Polls active orders from all providers in batches.
Updates status, records order events, notifies customers.
"""
from __future__ import annotations
from datetime import datetime, timezone
from app.core.logging import get_logger

logger = get_logger(__name__)


class OrderMonitor:
    def __init__(self, registry) -> None:
        self._registry = registry

    async def run_once(self, db) -> dict:
        from sqlalchemy import select
        from app.core.models import Order, User

        # Load all active orders grouped by provider
        stmt = (
            select(Order)
            .where(Order.status.in_(["pending", "processing", "in_progress"]))
            .where(Order.provider_order_id.isnot(None))
            .where(Order.provider_id.isnot(None))
            .limit(500)
        )
        orders = list((await db.execute(stmt)).scalars().all())
        if not orders:
            return {"checked": 0, "updated": 0, "errors": 0}

        # Group by provider for batch status checks
        by_provider: dict[int, list[Order]] = {}
        for o in orders:
            by_provider.setdefault(o.provider_id, []).append(o)

        updated = errors = 0
        now = datetime.now(timezone.utc)

        for provider_id, provider_orders in by_provider.items():
            try:
                prov_ids = [o.provider_order_id for o in provider_orders]
                statuses = await self._registry.get_multiple_order_status(
                    provider_id=provider_id,
                    provider_order_ids=prov_ids,
                )

                for order in provider_orders:
                    raw_status = statuses.get(order.provider_order_id)
                    if raw_status is None:
                        continue

                    new_status  = _map_status(raw_status.get("status", ""))
                    old_status  = order.status

                    if new_status and new_status != old_status:
                        order.status     = new_status
                        order.updated_at = now
                        if raw_status.get("remains") is not None:
                            order.remains = int(raw_status["remains"])
                        if raw_status.get("start_count") is not None:
                            order.start_count = int(raw_status["start_count"])
                        if new_status in ("completed", "partial", "failed", "cancelled"):
                            order.completed_at  = now
                            order.cancel_eligible = False

                        await _record_order_event(db, order.id, old_status, new_status)
                        await _notify_status_change(db, order, new_status)
                        updated += 1

            except Exception as exc:
                logger.warning("order_monitor_provider_error", provider_id=provider_id, error=str(exc))
                errors += 1

        await db.flush()
        logger.info("order_monitor_complete", checked=len(orders), updated=updated, errors=errors)
        return {"checked": len(orders), "updated": updated, "errors": errors}


def _map_status(raw: str) -> str | None:
    mapping = {
        "pending":     "pending",
        "in progress": "processing",
        "processing":  "processing",
        "completed":   "completed",
        "partial":     "partial",
        "cancelled":   "cancelled",
        "canceled":    "cancelled",
        "failed":      "failed",
    }
    return mapping.get(raw.lower().strip())


async def _record_order_event(db, order_id: str, from_status: str, to_status: str) -> None:
    try:
        from app.core.models import AuditLog
        from datetime import datetime, timezone
        db.add(AuditLog(
            action="order.status_change",
            resource="order",
            resource_id=order_id,
            details={"from": from_status, "to": to_status},
            created_at=datetime.now(timezone.utc),
        ))
    except Exception as exc:
        logger.debug("order_event_record_failed", error=str(exc))


async def _notify_status_change(db, order, new_status: str) -> None:
    try:
        from sqlalchemy import select
        from app.core.models import User
        from app.notifications.service import (
            notify_order_completed, notify_order_partial,
            notify_order_failed,
        )
        user = (await db.execute(select(User).where(User.id == order.user_id))).scalar_one_or_none()
        if not user:
            return
        if new_status == "completed":
            await notify_order_completed(user.telegram_id, order.public_ref)
        elif new_status == "partial":
            await notify_order_partial(user.telegram_id, order.public_ref, order.remains or 0)
        elif new_status == "failed":
            await notify_order_failed(user.telegram_id, order.public_ref, refunded=False)
    except Exception as exc:
        logger.debug("order_notify_failed", error=str(exc))
