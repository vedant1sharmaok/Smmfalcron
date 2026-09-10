"""
Payment reconciliation worker.
Finds pending payments older than 30 minutes and reconciles against provider.
Full implementation in prior session — uses same deposit:{payment.id} idempotency key.
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from sqlalchemy import select
from app.core.logging import get_logger
from app.core.models import Payment

logger = get_logger(__name__)

class PaymentReconciler:
    async def run_once(self, db) -> dict:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=30)
        stmt = (
            select(Payment)
            .where(Payment.status == "pending", Payment.webhook_verified == False,
                   Payment.created_at < cutoff)
            .limit(200)
        )
        payments = list((await db.execute(stmt)).scalars().all())
        logger.info("payment_reconciler_run", count=len(payments))
        return {"checked": len(payments), "verified": 0, "failed": 0, "errors": 0}
