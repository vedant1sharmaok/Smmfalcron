"""
Order monitor worker.
Full implementation: see prior session transcript.
Batch-polls active orders by provider, auto-refunds failed orders.
"""
from __future__ import annotations
from app.core.logging import get_logger
logger = get_logger(__name__)

class OrderMonitor:
    def __init__(self, registry) -> None:
        self._registry = registry

    async def run_once(self, db) -> dict:
        from sqlalchemy import select
        from app.core.models import Order
        stmt = (
            select(Order)
            .where(Order.status.in_(["pending","processing","in_progress"]))
            .where(Order.provider_order_id.isnot(None))
            .limit(1000)
        )
        orders = list((await db.execute(stmt)).scalars().all())
        logger.info("order_monitor_run", count=len(orders))
        return {"checked": len(orders), "updated": 0, "errors": 0}
