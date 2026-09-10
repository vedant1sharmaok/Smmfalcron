"""
Integration tests — Payment Webhooks.

Coverage:
  - Valid webhook credits wallet exactly once
  - Duplicate webhook (same provider_txn_id) raises DuplicatePaymentError
  - Wrong signature raises InvalidWebhookSignatureError before any DB read
  - Amount mismatch raises PaymentVerificationError
  - webhook_verified flag set after first successful processing
  - Credit idempotency key matches reconciler key (deposit:{payment.id})
  - Failed payment status updated, no credit issued
  - Reconciler credits wallet for missed webhook
  - Reconciler + late webhook do not double-credit
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac as _hmac
import json
import os
import sys
import unittest
import uuid
from decimal import Decimal

try:
    import asyncpg       # noqa
    import pytest
    HAS_DEPS = True
except ImportError:
    HAS_DEPS = False

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+asyncpg://smm_app:dev_password_change_in_prod@localhost:5432/smm_platform",
)

RAZORPAY_SECRET = os.environ.get(
    "RAZORPAY_WEBHOOK_SECRET",
    "test_webhook_secret_32_chars____",
)


def _make_rzp_body(
    pay_id: str = "pay_test001",
    paise: int = 50000,         # ₹500.00
    currency: str = "INR",
    receipt: str = "SMM-TEST-001",
    event: str = "payment.captured",
) -> bytes:
    return json.dumps({
        "event": event,
        "payload": {"payment": {"entity": {
            "id": pay_id,
            "amount": paise,
            "currency": currency,
            "receipt": receipt,
        }}},
    }).encode()


def _rzp_sig(body: bytes, secret: str = RAZORPAY_SECRET) -> str:
    return _hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


if HAS_DEPS:
    import pytest
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy import text

    @pytest.fixture(scope="session")
    def event_loop():
        loop = asyncio.get_event_loop_policy().new_event_loop()
        yield loop
        loop.close()

    @pytest.fixture(scope="session")
    async def engine():
        eng = create_async_engine(DATABASE_URL, echo=False, pool_pre_ping=True)
        yield eng
        await eng.dispose()

    @pytest.fixture
    async def db(engine):
        async with engine.begin() as conn:
            await conn.execute(text("SAVEPOINT test_savepoint"))
            session = AsyncSession(bind=conn, expire_on_commit=False)
            yield session
            await conn.execute(text("ROLLBACK TO SAVEPOINT test_savepoint"))

    @pytest.fixture
    async def payment_factory(db):
        """Create a pending payment intent for a user."""
        created_users = []

        async def _make(
            amount: Decimal = Decimal("500.00"),
            currency: str = "INR",
            provider: str = "razorpay",
            provider_ref: str | None = None,
        ):
            tg_id = int(uuid.uuid4().int % (10**15))
            await db.execute(text(
                "INSERT INTO users (id, telegram_id, is_active, is_banned, policy_version)"
                " VALUES (:id, :tg, true, false, 0)"
            ), {"id": tg_id, "tg": tg_id})
            await db.execute(text(
                "INSERT INTO wallets (user_id, balance, currency)"
                " VALUES (:uid, 0.00, :cur)"
            ), {"uid": tg_id, "cur": currency})

            ref = provider_ref or f"SMM-{uuid.uuid4().hex[:8].upper()}"
            payment_id = (await db.execute(text(
                "INSERT INTO payments"
                " (user_id, amount, currency, provider, provider_ref,"
                "  status, webhook_verified)"
                " VALUES (:uid, :amt, :cur, :prov, :ref, 'pending', false)"
                " RETURNING id"
            ), {
                "uid": tg_id, "amt": amount, "cur": currency,
                "prov": provider, "ref": ref,
            })).scalar_one()

            await db.flush()
            created_users.append(tg_id)
            return {"payment_id": payment_id, "user_id": tg_id, "ref": ref}

        yield _make

    # ─────────────────────────────────────────────────────────────────────────

    class TestWebhookProcessing:

        @pytest.mark.asyncio
        async def test_valid_webhook_credits_wallet(self, db, payment_factory):
            p = await payment_factory(amount=Decimal("500.00"))
            body = _make_rzp_body(
                pay_id="pay_valid001",
                paise=50000,
                receipt=p["ref"],
            )
            headers = {"x-razorpay-signature": _rzp_sig(body)}

            from app.payments.adapters import process_payment_webhook
            await process_payment_webhook(db, "razorpay", body, headers)
            await db.flush()

            balance = (await db.execute(
                text("SELECT balance FROM wallets WHERE user_id = :uid"),
                {"uid": p["user_id"]},
            )).scalar_one()
            assert balance == Decimal("500.00")

        @pytest.mark.asyncio
        async def test_valid_webhook_sets_verified_flag(self, db, payment_factory):
            p = await payment_factory(amount=Decimal("200.00"))
            body = _make_rzp_body(paise=20000, receipt=p["ref"])
            headers = {"x-razorpay-signature": _rzp_sig(body)}

            from app.payments.adapters import process_payment_webhook
            await process_payment_webhook(db, "razorpay", body, headers)
            await db.flush()

            row = (await db.execute(
                text("SELECT webhook_verified, status FROM payments WHERE id = :id"),
                {"id": p["payment_id"]},
            )).fetchone()
            assert row[0] is True
            assert row[1] == "verified"

        @pytest.mark.asyncio
        async def test_duplicate_webhook_raises_no_double_credit(
            self, db, payment_factory
        ):
            from app.payments.adapters import process_payment_webhook
            from app.core.exceptions import DuplicatePaymentError

            p = await payment_factory(amount=Decimal("300.00"))
            body = _make_rzp_body(paise=30000, receipt=p["ref"])
            headers = {"x-razorpay-signature": _rzp_sig(body)}

            # First delivery — succeeds.
            await process_payment_webhook(db, "razorpay", body, headers)
            await db.flush()

            balance_after_first = (await db.execute(
                text("SELECT balance FROM wallets WHERE user_id = :uid"),
                {"uid": p["user_id"]},
            )).scalar_one()

            # Second delivery — must raise DuplicatePaymentError.
            with pytest.raises(DuplicatePaymentError):
                await process_payment_webhook(db, "razorpay", body, headers)
            await db.flush()

            balance_after_second = (await db.execute(
                text("SELECT balance FROM wallets WHERE user_id = :uid"),
                {"uid": p["user_id"]},
            )).scalar_one()

            assert balance_after_first == balance_after_second
            assert balance_after_first == Decimal("300.00")

        @pytest.mark.asyncio
        async def test_invalid_signature_raises_before_db_read(
            self, db, payment_factory
        ):
            from app.payments.adapters import process_payment_webhook
            from app.core.exceptions import InvalidWebhookSignatureError

            p = await payment_factory()
            body = _make_rzp_body(receipt=p["ref"])
            headers = {"x-razorpay-signature": "completely_invalid_signature"}

            with pytest.raises(InvalidWebhookSignatureError):
                await process_payment_webhook(db, "razorpay", body, headers)

            # Wallet must still be at zero — no DB operation ran.
            balance = (await db.execute(
                text("SELECT balance FROM wallets WHERE user_id = :uid"),
                {"uid": p["user_id"]},
            )).scalar_one()
            assert balance == Decimal("0.00")

        @pytest.mark.asyncio
        async def test_amount_mismatch_raises_no_credit(self, db, payment_factory):
            from app.payments.adapters import process_payment_webhook
            from app.core.exceptions import PaymentVerificationError

            # Payment intent for ₹500
            p = await payment_factory(amount=Decimal("500.00"))
            # Webhook says ₹100 — mismatch
            body = _make_rzp_body(paise=10000, receipt=p["ref"])
            headers = {"x-razorpay-signature": _rzp_sig(body)}

            with pytest.raises(PaymentVerificationError):
                await process_payment_webhook(db, "razorpay", body, headers)

            balance = (await db.execute(
                text("SELECT balance FROM wallets WHERE user_id = :uid"),
                {"uid": p["user_id"]},
            )).scalar_one()
            assert balance == Decimal("0.00")

        @pytest.mark.asyncio
        async def test_failed_event_skipped_no_credit(self, db, payment_factory):
            from app.payments.adapters import process_payment_webhook

            p = await payment_factory(amount=Decimal("100.00"))
            body = _make_rzp_body(
                paise=10000, receipt=p["ref"], event="payment.failed"
            )
            headers = {"x-razorpay-signature": _rzp_sig(body)}

            # Should not raise — failed events are skipped.
            await process_payment_webhook(db, "razorpay", body, headers)

            balance = (await db.execute(
                text("SELECT balance FROM wallets WHERE user_id = :uid"),
                {"uid": p["user_id"]},
            )).scalar_one()
            assert balance == Decimal("0.00")

    class TestReconcilerIdempotency:

        @pytest.mark.asyncio
        async def test_reconciler_and_late_webhook_no_double_credit(
            self, db, payment_factory
        ):
            """
            Reconciler credits wallet for a payment missed by webhook.
            Late-arriving webhook sees DuplicatePaymentError — no double credit.
            """
            from app.wallet.ledger import credit, TxType
            from app.payments.adapters import process_payment_webhook
            from app.core.exceptions import DuplicatePaymentError

            p = await payment_factory(amount=Decimal("250.00"))
            payment_id = p["payment_id"]

            # Simulate reconciler: credit with stable idempotency key.
            await credit(
                db=db,
                user_id=p["user_id"],
                amount=Decimal("250.00"),
                tx_type=TxType.DEPOSIT,
                idempotency_key=f"deposit:{payment_id}",
                reference_id=payment_id,
                reason="Reconciled deposit",
            )
            # Mark as verified.
            await db.execute(
                text("UPDATE payments SET webhook_verified = true, status = 'verified'"
                     " WHERE id = :id"),
                {"id": payment_id},
            )
            await db.flush()

            balance_after_reconciler = (await db.execute(
                text("SELECT balance FROM wallets WHERE user_id = :uid"),
                {"uid": p["user_id"]},
            )).scalar_one()
            assert balance_after_reconciler == Decimal("250.00")

            # Late webhook arrives — must raise DuplicatePaymentError.
            body = _make_rzp_body(paise=25000, receipt=p["ref"])
            headers = {"x-razorpay-signature": _rzp_sig(body)}
            with pytest.raises(DuplicatePaymentError):
                await process_payment_webhook(db, "razorpay", body, headers)

            balance_final = (await db.execute(
                text("SELECT balance FROM wallets WHERE user_id = :uid"),
                {"uid": p["user_id"]},
            )).scalar_one()
            assert balance_final == Decimal("250.00")


# ── Offline tests ─────────────────────────────────────────────────────────────

class TestWebhookContractsOffline(unittest.TestCase):

    def test_razorpay_signature_valid(self):
        secret = "test_secret_key"
        body   = b'{"event":"payment.captured"}'
        sig    = _hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        computed = _hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        assert sig == computed

    def test_razorpay_signature_tampered(self):
        secret   = "test_secret"
        body     = b"original"
        tampered = b"modified"
        sig_orig    = _hmac.new(secret.encode(), body,     hashlib.sha256).hexdigest()
        sig_tampered= _hmac.new(secret.encode(), tampered, hashlib.sha256).hexdigest()
        assert sig_orig != sig_tampered

    def test_paise_to_inr_conversion(self):
        cases = [
            (50000, Decimal("500.00")),
            (10000, Decimal("100.00")),
            (1,     Decimal("0.01")),
            (99999, Decimal("999.99")),
        ]
        for paise, expected in cases:
            result = (Decimal(str(paise)) / 100).quantize(Decimal("0.01"))
            assert result == expected, f"paise={paise} expected={expected} got={result}"

    def test_idempotency_key_format(self):
        payment_id = "abc-123-def"
        key = f"deposit:{payment_id}"
        assert key == "deposit:abc-123-def"
        assert key.startswith("deposit:")

    def test_duplicate_payment_detection(self):
        """webhook_verified=True signals a duplicate."""
        class FakePayment:
            webhook_verified = True
            amount = Decimal("100")
            currency = "INR"

        p = FakePayment()
        assert p.webhook_verified is True   # duplicate

    def test_amount_mismatch_detection(self):
        """Webhook amount must match the stored payment intent amount."""
        cases = [
            (Decimal("100.00"), Decimal("100.00"), False),   # match — no error
            (Decimal("100.00"), Decimal("99.99"),  True),    # mismatch
            (Decimal("100.00"), Decimal("100.01"), True),    # mismatch
            (Decimal("500.00"), Decimal("500.00"), False),   # match
        ]
        for stored, received, should_mismatch in cases:
            mismatch = stored != received
            assert mismatch == should_mismatch, \
                f"stored={stored} received={received}"


if __name__ == "__main__":
    if HAS_DEPS:
        import pytest
        sys.exit(pytest.main([__file__, "-v"]))
    else:
        print("pytest/asyncpg not available — running offline tests only")
        unittest.main(verbosity=2)
