"""
Payment reconciler — Section 18-19 of blueprint.
Finds pending payments older than 30 min and attempts to verify them
against provider. Uses same deposit:{payment.id} idempotency key
as the webhook handler to prevent double-credit.
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from sqlalchemy import select
from app.core.models import Payment
from app.core.logging import get_logger

logger = get_logger(__name__)


class PaymentReconciler:
    async def run_once(self, db) -> dict:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=30)
        stmt   = (
            select(Payment)
            .where(
                Payment.status == "pending",
                Payment.webhook_verified == False,
                Payment.created_at < cutoff,
            )
            .limit(100)
        )
        payments = list((await db.execute(stmt)).scalars().all())
        if not payments:
            return {"checked": 0, "verified": 0, "failed": 0, "errors": 0}

        verified = failed = errors = 0
        for payment in payments:
            try:
                result = await self._check_provider(payment)
                if result == "verified":
                    await self._credit_wallet(db, payment)
                    verified += 1
                elif result == "failed":
                    payment.status = "failed"
                    failed += 1
            except Exception as exc:
                logger.warning("reconciler_payment_error", payment_id=payment.id, error=str(exc))
                errors += 1

        await db.flush()
        logger.info("payment_reconciler_complete",
                    checked=len(payments), verified=verified, failed=failed, errors=errors)
        return {"checked": len(payments), "verified": verified, "failed": failed, "errors": errors}

    async def _check_provider(self, payment: Payment) -> str:
        """Check payment status with provider. Returns 'verified' | 'failed' | 'pending'."""
        # In production: call Razorpay/Stripe API to check payment status
        # For now: leave pending (webhook will arrive)
        return "pending"

    async def _credit_wallet(self, db, payment: Payment) -> None:
        """Credit wallet using same idempotency key as webhook handler."""
        import uuid
        from app.wallet.ledger import credit, TxType
        from app.notifications.service import notify_deposit_success
        from sqlalchemy import select
        from app.core.models import User, Wallet

        await credit(
            db=db,
            user_id=payment.user_id,
            amount=payment.amount,
            tx_type=TxType.DEPOSIT,
            reference_id=payment.id,
            reason=f"Reconciled {payment.provider} payment",
            idempotency_key=f"deposit:{payment.id}",
        )
        payment.webhook_verified = True
        payment.status           = "verified"
        payment.verified_at      = datetime.now(timezone.utc)

        # Notify customer
        try:
            from sqlalchemy import select
            from app.core.models import User, Wallet
            user   = (await db.execute(select(User).where(User.id == payment.user_id))).scalar_one_or_none()
            wallet = (await db.execute(select(Wallet).where(Wallet.user_id == payment.user_id))).scalar_one_or_none()
            if user and wallet:
                await notify_deposit_success(user.telegram_id, payment.amount, wallet.balance)
        except Exception:
            pass
