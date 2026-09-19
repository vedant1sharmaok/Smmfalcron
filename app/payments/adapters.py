"""
Payment adapters — base class + Razorpay implementation.

Webhook processing pipeline (10 steps):
  1.  Verify signature BEFORE any DB operation
  2.  Parse event type — skip non-payment events
  3.  Extract payment info (amount, currency, reference)
  4.  Load Payment record by provider_ref
  5.  Check DuplicatePaymentError (webhook_verified == True)
  6.  Verify amount matches stored payment intent
  7.  Verify currency matches
  8.  Credit wallet (idempotency_key = f"deposit:{payment.id}")
  9.  Mark payment as verified (webhook_verified = True)
  10. Flush and return

Security:
  - Signature verified BEFORE any DB read (prevents timing oracle)
  - wallet credit uses stable idempotency_key shared with reconciler
  - DuplicatePaymentError on webhook_verified=True prevents double-credit
  - Amount mismatch raises PaymentVerificationError (no credit)
"""

from __future__ import annotations

import hashlib
import hmac as _hmac
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    DuplicatePaymentError,
    InvalidWebhookSignatureError,
    PaymentVerificationError,
)
from app.core.logging import get_logger
from app.core.models import Payment
from app.wallet.ledger import TxType, credit

logger = get_logger(__name__)

_ZERO = Decimal("0")


@dataclass
class PaymentIntentResult:
    """Result of creating a payment intent."""
    provider_txn_id: str
    client_secret:   str
    amount:          Decimal
    currency:        str
    reference:       str
    provider:        str


class BasePaymentAdapter:
    """Abstract base for payment providers."""

    def verify_webhook_signature(self, body: bytes, headers: dict[str, str]) -> bool:
        raise NotImplementedError

    def extract_payment_info(self, body: bytes, headers: dict[str, str]) -> dict:
        raise NotImplementedError

    async def create_payment_intent(
        self, db: AsyncSession, user_id: int, amount: Decimal,
        currency: str = "INR", smm_ref: str = "", idempotency_key: str = "",
    ) -> PaymentIntentResult:
        raise NotImplementedError


class RazorpayAdapter(BasePaymentAdapter):
    """
    Razorpay webhook adapter.
    Reads settings.razorpay_webhook_secret for HMAC-SHA256 verification.
    """

    def verify_webhook_signature(
        self, body: bytes, headers: dict[str, str]
    ) -> bool:
        """
        Verify Razorpay webhook signature.
        Header: X-Razorpay-Signature: HMAC-SHA256(webhook_secret, raw_body)
        """
        from app.core.config import settings
        secret = getattr(settings, "razorpay_webhook_secret", "")
        if not secret:
            logger.error("razorpay_webhook_secret_not_configured")
            return False

        sig = (
            headers.get("x-razorpay-signature")
            or headers.get("X-Razorpay-Signature")
            or ""
        )
        if not sig:
            return False

        expected = _hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return _hmac.compare_digest(expected, sig)

    def extract_payment_info(
        self, body: bytes, headers: dict[str, str]
    ) -> dict[str, Any]:
        """
        Extract payment details from Razorpay webhook payload.
        Returns dict with: status, provider_txn_id, amount, currency, reference.
        status = 'captured' | 'failed' | 'skip'
        """
        try:
            payload = json.loads(body)
        except Exception:
            return {"status": "skip"}

        event = payload.get("event", "")
        if event != "payment.captured":
            return {"status": "skip"}

        entity = payload.get("payload", {}).get("payment", {}).get("entity", {})
        paise  = entity.get("amount", 0)

        return {
            "status":          "captured",
            "provider_txn_id": entity.get("id", ""),
            "amount":          (Decimal(str(paise)) / 100).quantize(Decimal("0.01")),
            "currency":        entity.get("currency", "INR"),
            "reference":       entity.get("receipt", ""),
        }


async def process_payment_webhook(
    db: AsyncSession,
    provider: str,
    body: bytes,
    headers: dict[str, str],
) -> None:
    """
    Process an incoming payment webhook delivery.
    Implements the 10-step pipeline with full idempotency.
    """
    # Select adapter
    if provider == "razorpay":
        adapter = RazorpayAdapter()
    else:
        logger.warning("payment_webhook_unknown_provider", provider=provider)
        return

    # Step 1: Verify signature BEFORE any DB operation
    if not adapter.verify_webhook_signature(body, headers):
        raise InvalidWebhookSignatureError(
            detail=f"{provider} webhook signature verification failed",
        )

    # Step 2-3: Extract payment info
    info = adapter.extract_payment_info(body, headers)
    if info["status"] == "skip":
        return
    if info["status"] == "failed":
        return

    # Step 4: Load payment by reference
    payment = (await db.execute(
        select(Payment).where(
            Payment.provider_ref == info["reference"],
            Payment.provider == provider,
        )
    )).scalar_one_or_none()

    if payment is None:
        logger.warning(
            "payment_webhook_ref_not_found",
            reference=info["reference"],
            provider=provider,
        )
        return

    # Step 5: Duplicate protection
    if payment.webhook_verified:
        raise DuplicatePaymentError(
            detail=f"Webhook already processed for payment {payment.id}",
        )

    # Step 6: Amount verification
    received = info["amount"]
    if abs(received - payment.amount) > Decimal("0.01"):
        raise PaymentVerificationError(
            detail=(
                f"Amount mismatch: expected={payment.amount} "
                f"received={received} payment_id={payment.id}"
            ),
        )

    # Step 7: Currency verification
    if info["currency"].upper() != payment.currency.upper():
        raise PaymentVerificationError(
            detail=f"Currency mismatch: expected={payment.currency} received={info['currency']}",
        )

    # Step 8: Credit wallet with stable idempotency key
    await credit(
        db=db,
        user_id=payment.user_id,
        amount=payment.amount,
        tx_type=TxType.DEPOSIT,
        reference_id=payment.id,
        reason=f"{provider} deposit — {info['provider_txn_id']}",
        idempotency_key=f"deposit:{payment.id}",
    )

    # Steps 9-10: Mark verified and flush
    payment.webhook_verified = True
    payment.status           = "verified"
    payment.provider_txn_id  = info["provider_txn_id"]
    payment.verified_at      = datetime.now(timezone.utc)
    await db.flush()

    logger.info(
        "payment_webhook_processed",
        payment_id=payment.id,
        provider=provider,
        provider_txn_id=info["provider_txn_id"],
        amount=str(payment.amount),
    )
