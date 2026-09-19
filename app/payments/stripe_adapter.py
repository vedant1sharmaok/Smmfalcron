"""
Stripe payment adapter.

Implements BasePaymentAdapter for Stripe.
Mirrors the RazorpayAdapter interface exactly so it drops into the
payment pipeline without changes to the order engine or webhook router.

Webhook verification:
  Stripe uses a signed timestamp + payload scheme (Stripe-Signature header).
  The signature is HMAC-SHA256 of:  f"{timestamp}.{payload}"
  using the webhook endpoint's signing secret.
  Tolerance: ±300 seconds (Stripe default).

Supported events:
  payment_intent.succeeded   → credit wallet
  payment_intent.canceled    → mark failed
  checkout.session.completed → credit wallet (Checkout flow)
  All others                 → skip

Idempotency:
  Uses Stripe's PaymentIntent.id as provider_txn_id.
  Credit idempotency_key: f"deposit:{payment.id}" — same as Razorpay path
  so the reconciler key is identical across providers.

Payment Intent creation:
  Creates a Stripe PaymentIntent in INR (or configured currency).
  Returns client_secret for the frontend to complete payment.
  The receipt_email and metadata.smm_ref are set for reconciliation.
"""

from __future__ import annotations

import hashlib
import hmac as _hmac
import json
import time
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import (
    DuplicatePaymentError,
    InvalidWebhookSignatureError,
    PaymentVerificationError,
    ProviderResponseError,
)
from app.core.logging import get_logger
from app.payments.adapters import BasePaymentAdapter, PaymentIntentResult
from app.wallet.ledger import TxType, credit

logger = get_logger(__name__)

_SIGNATURE_TOLERANCE_S = 300   # Stripe default: ±5 minutes
_ZERO = Decimal("0")


class StripeAdapter(BasePaymentAdapter):
    """
    Stripe payment adapter.
    Reads credentials from settings:
      settings.stripe_secret_key          — sk_live_... or sk_test_...
      settings.stripe_webhook_secret      — whsec_...
      settings.stripe_currency            — default 'inr'
    """

    def verify_webhook_signature(
        self, body: bytes, headers: dict[str, str]
    ) -> bool:
        """
        Verify Stripe webhook signature.

        Stripe-Signature header format:
          t=<timestamp>,v1=<signature>[,v0=<old_sig>]

        Verification:
          signed_payload = f"{timestamp}.{raw_body}"
          expected_sig   = HMAC-SHA256(webhook_secret, signed_payload)
          compare expected_sig with each v1 signature in the header
        """
        sig_header = (
            headers.get("stripe-signature")
            or headers.get("Stripe-Signature")
            or ""
        )
        if not sig_header:
            return False

        webhook_secret = getattr(settings, "stripe_webhook_secret", "")
        if not webhook_secret:
            logger.error("stripe_webhook_secret_not_configured")
            return False

        # Parse the Stripe-Signature header
        timestamp_s: str | None = None
        v1_sigs: list[str] = []
        for part in sig_header.split(","):
            part = part.strip()
            if part.startswith("t="):
                timestamp_s = part[2:]
            elif part.startswith("v1="):
                v1_sigs.append(part[3:])

        if not timestamp_s or not v1_sigs:
            return False

        # Replay protection: reject if timestamp is too old.
        try:
            timestamp_int = int(timestamp_s)
        except ValueError:
            return False

        age = abs(int(time.time()) - timestamp_int)
        if age > _SIGNATURE_TOLERANCE_S:
            logger.warning(
                "stripe_webhook_stale",
                age_seconds=age,
                tolerance=_SIGNATURE_TOLERANCE_S,
            )
            return False

        # Compute expected signature.
        signed_payload = f"{timestamp_s}.{body.decode('utf-8', errors='replace')}"
        expected = _hmac.new(
            webhook_secret.encode(),
            signed_payload.encode(),
            hashlib.sha256,
        ).hexdigest()

        # Compare against all v1 signatures (Stripe may send multiple).
        return any(
            _hmac.compare_digest(expected, sig)
            for sig in v1_sigs
        )

    def extract_payment_info(
        self, body: bytes, headers: dict[str, str]
    ) -> dict:
        """
        Extract payment information from a Stripe webhook payload.

        Returns dict with keys:
          status         — 'captured' | 'failed' | 'skip'
          provider_txn_id — PaymentIntent.id (pi_...)
          amount         — Decimal (in INR/major currency unit)
          currency       — uppercase currency code
          reference      — metadata.smm_ref (our internal payment ref)
        """
        try:
            payload = json.loads(body)
        except Exception:
            return {"status": "skip"}

        event_type = payload.get("type", "")

        # Events we handle
        if event_type == "payment_intent.succeeded":
            pi = payload.get("data", {}).get("object", {})
            return self._extract_from_payment_intent(pi, "captured")

        elif event_type == "checkout.session.completed":
            session = payload.get("data", {}).get("object", {})
            # payment_status must be 'paid' for credit
            if session.get("payment_status") != "paid":
                return {"status": "skip"}
            pi_id = session.get("payment_intent", "")
            amount_total = session.get("amount_total", 0)
            currency = session.get("currency", "inr").upper()
            smm_ref = (session.get("metadata") or {}).get("smm_ref", "")
            return {
                "status":          "captured",
                "provider_txn_id": pi_id,
                "amount":          self._stripe_amount_to_decimal(amount_total, currency),
                "currency":        currency,
                "reference":       smm_ref,
            }

        elif event_type == "payment_intent.canceled":
            pi = payload.get("data", {}).get("object", {})
            return {"status": "failed", "provider_txn_id": pi.get("id", ""),
                    "amount": _ZERO, "currency": "INR", "reference": ""}

        else:
            return {"status": "skip"}

    def _extract_from_payment_intent(
        self, pi: dict, status: str
    ) -> dict:
        """Extract fields from a PaymentIntent object."""
        amount_received = pi.get("amount_received") or pi.get("amount", 0)
        currency        = (pi.get("currency") or "inr").upper()
        smm_ref         = (pi.get("metadata") or {}).get("smm_ref", "")
        return {
            "status":          status,
            "provider_txn_id": pi.get("id", ""),
            "amount":          self._stripe_amount_to_decimal(amount_received, currency),
            "currency":        currency,
            "reference":       smm_ref,
        }

    @staticmethod
    def _stripe_amount_to_decimal(amount_minor: int, currency: str) -> Decimal:
        """
        Convert Stripe amount (minor units) to Decimal major units.
        Most currencies: amount / 100
        Zero-decimal currencies (JPY, KRW, etc.): amount as-is
        """
        _ZERO_DECIMAL = {"BIF","CLP","DJF","GNF","JPY","KMF","KRW",
                         "MGA","PYG","RWF","UGX","VND","VUV","XAF","XOF","XPF"}
        if currency.upper() in _ZERO_DECIMAL:
            return Decimal(str(amount_minor)).quantize(Decimal("1"))
        return (Decimal(str(amount_minor)) / 100).quantize(Decimal("0.01"))

    async def create_payment_intent(
        self,
        db: AsyncSession,
        user_id: int,
        amount: Decimal,
        currency: str = "INR",
        smm_ref: str = "",
        idempotency_key: str = "",
    ) -> PaymentIntentResult:
        """
        Create a Stripe PaymentIntent.
        Returns PaymentIntentResult with client_secret for the frontend.

        The frontend uses the client_secret with Stripe.js to complete payment.
        """
        secret_key = getattr(settings, "stripe_secret_key", "")
        if not secret_key:
            raise ProviderResponseError(
                detail="Stripe secret key not configured",
                provider_id=0,
            )

        try:
            import httpx
            currency_lower = currency.lower()
            amount_minor   = int(amount * 100)   # convert to paise/cents

            headers = {
                "Authorization": f"Bearer {secret_key}",
                "Content-Type":  "application/x-www-form-urlencoded",
            }
            if idempotency_key:
                headers["Idempotency-Key"] = idempotency_key

            data = {
                "amount":                  str(amount_minor),
                "currency":                currency_lower,
                "payment_method_types[]":  "card",
                "metadata[smm_ref]":       smm_ref,
                "metadata[user_id]":       str(user_id),
                "description":             f"SMM Platform deposit — {smm_ref}",
            }

            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    "https://api.stripe.com/v1/payment_intents",
                    data=data,
                    headers=headers,
                )

            if resp.status_code not in (200, 201):
                error_msg = resp.json().get("error", {}).get("message", "Unknown error")
                raise ProviderResponseError(
                    detail=f"Stripe PaymentIntent creation failed: {error_msg}",
                    provider_id=0,
                )

            pi = resp.json()
            return PaymentIntentResult(
                provider_txn_id=pi["id"],
                client_secret=pi.get("client_secret", ""),
                amount=amount,
                currency=currency,
                reference=smm_ref,
                provider="stripe",
            )

        except ImportError:
            raise ProviderResponseError(
                detail="httpx not installed — required for Stripe API calls",
                provider_id=0,
            )

    async def retrieve_payment_intent(self, pi_id: str) -> dict:
        """
        Retrieve a PaymentIntent by ID from Stripe (for reconciliation).
        Returns the PaymentIntent object as a dict.
        """
        secret_key = getattr(settings, "stripe_secret_key", "")
        if not secret_key:
            return {}

        try:
            import httpx
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(
                    f"https://api.stripe.com/v1/payment_intents/{pi_id}",
                    headers={"Authorization": f"Bearer {secret_key}"},
                )
            if resp.status_code == 200:
                return resp.json()
            return {}
        except Exception as exc:
            logger.error("stripe_retrieve_error", pi_id=pi_id, error=str(exc))
            return {}


async def process_stripe_webhook(
    db: AsyncSession,
    body: bytes,
    headers: dict[str, str],
) -> None:
    """
    Process a Stripe webhook delivery.
    Entry point called by the webhook route handler.
    Mirrors process_payment_webhook() from adapters.py for Razorpay.
    """
    from app.core.models import Payment

    adapter = StripeAdapter()

    # Step 1: Verify signature BEFORE any DB operation.
    if not adapter.verify_webhook_signature(body, headers):
        raise InvalidWebhookSignatureError(
            detail="Stripe webhook signature verification failed",
        )

    # Step 2: Extract payment info.
    info = adapter.extract_payment_info(body, headers)
    if info["status"] == "skip":
        logger.info("stripe_webhook_skipped", event_type="unknown")
        return

    if info["status"] == "failed":
        # Mark payment as failed in DB.
        from sqlalchemy import select
        payment = (await db.execute(
            select(Payment).where(Payment.provider_ref == info.get("reference", ""))
        )).scalar_one_or_none()
        if payment:
            payment.status = "failed"
            await db.flush()
        return

    # Step 3: Captured — find the payment intent by smm_ref.
    from sqlalchemy import select
    payment = (await db.execute(
        select(Payment).where(
            Payment.provider_ref == info["reference"],
            Payment.provider == "stripe",
        )
    )).scalar_one_or_none()

    if payment is None:
        logger.warning(
            "stripe_webhook_payment_not_found",
            reference=info["reference"],
            provider_txn_id=info["provider_txn_id"],
        )
        return

    # Step 4: Duplicate protection.
    if payment.webhook_verified:
        raise DuplicatePaymentError(
            detail=f"Stripe webhook already processed for payment {payment.id}",
        )

    # Step 5: Amount verification.
    received = info["amount"]
    if abs(received - payment.amount) > Decimal("0.01"):
        raise PaymentVerificationError(
            detail=(
                f"Stripe amount mismatch: expected={payment.amount}"
                f" received={received} payment_id={payment.id}"
            ),
        )

    # Step 6: Credit wallet (idempotent — same key as Razorpay + reconciler).
    await credit(
        db=db,
        user_id=payment.user_id,
        amount=payment.amount,
        tx_type=TxType.DEPOSIT,
        reference_id=payment.id,
        reason=f"Stripe deposit — {info['provider_txn_id']}",
        idempotency_key=f"deposit:{payment.id}",
    )

    # Step 7: Mark verified.
    payment.webhook_verified  = True
    payment.status            = "verified"
    payment.provider_txn_id   = info["provider_txn_id"]
    payment.verified_at       = __import__('datetime').datetime.now(
        __import__('datetime').timezone.utc
    )
    await db.flush()

    logger.info(
        "stripe_payment_verified",
        payment_id=payment.id,
        provider_txn_id=info["provider_txn_id"],
        amount=str(payment.amount),
    )
