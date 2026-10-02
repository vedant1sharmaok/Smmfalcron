"""Money-path integration tests (SQLite). These exercise the real services against a real schema.

    pip install -r requirements-dev.txt && pytest -q
"""

import pytest

from app.config import get_settings
from app.db import session_scope
from app.models import LedgerEntry, Payment, PaymentEvent, User
from app.payments.base import Checkout, GatewayEvent
from app.payments.service import (
    DepositError,
    create_deposit,
    handle_webhook_event,
    settle_payment,
)
from app.security import utcnow
from app.wallet import InsufficientFunds, credit, reserve
from sqlalchemy import func, select

pytestmark = pytest.mark.asyncio


class FakeGateway:
    name = "razorpay"

    async def create_checkout(self, **kw):
        return Checkout(url="https://pay.example.com/x", gateway_ref="plink_TEST123456")

    def parse_webhook(self, headers, body):  # not used here
        raise NotImplementedError

    async def fetch_status(self, ref):  # not used here
        raise NotImplementedError


async def _make_user(session, uid=1001, terms=True) -> User:
    user = User(telegram_id=uid, first_name="T", terms_accepted_at=utcnow() if terms else None)
    session.add(user)
    await session.flush()
    return user


def _event(payment, *, event_id="evt_1", amount=None, currency="INR", ref="plink_TEST123456"):
    return GatewayEvent(
        event_id=event_id,
        event_type="payment_link.paid",
        kind="paid",
        reference=payment.public_id,
        gateway_ref=ref,
        amount_paise=payment.amount_paise if amount is None else amount,
        currency=currency,
    )


async def _balance(uid):
    async with session_scope() as s:
        return int((await s.get(User, uid)).cached_balance_paise)


async def test_webhook_credits_exactly_once_even_when_redelivered(db):
    async with session_scope() as s:
        user = await _make_user(s)
        payment = await create_deposit(s, user, 50_000, settings=get_settings(), gateway=FakeGateway())
        assert payment.status == "pending" and payment.checkout_url
        pid = payment.public_id

    async with session_scope() as s:
        p = (await s.execute(select(Payment).where(Payment.public_id == pid))).scalar_one()
        first = await handle_webhook_event(s, "razorpay", _event(p))
    async with session_scope() as s:
        p = (await s.execute(select(Payment).where(Payment.public_id == pid))).scalar_one()
        replay = await handle_webhook_event(s, "razorpay", _event(p))              # same event id
        other = await handle_webhook_event(s, "razorpay", _event(p, event_id="evt_2"))  # new id, same payment

    assert first.outcome == "credited"
    assert replay.outcome == "duplicate"
    assert other.outcome == "already_paid"
    assert await _balance(1001) == 50_000
    async with session_scope() as s:
        credits = (await s.execute(select(func.count()).select_from(LedgerEntry).where(
            LedgerEntry.user_id == 1001, LedgerEntry.entry_type == "credit"))).scalar_one()
        events = (await s.execute(select(func.count()).select_from(PaymentEvent))).scalar_one()
    assert credits == 1
    assert events == 2  # evt_1 and evt_2; the replay did not add a row


async def test_amount_or_currency_mismatch_is_held_not_credited(db):
    async with session_scope() as s:
        user = await _make_user(s)
        payment = await create_deposit(s, user, 50_000, settings=get_settings(), gateway=FakeGateway())
        pid = payment.public_id
    async with session_scope() as s:
        p = (await s.execute(select(Payment).where(Payment.public_id == pid))).scalar_one()
        out = await settle_payment(s, payment_public_id=pid, gateway="razorpay", gateway_ref=p.provider_ref,
                                   amount_paise=100, currency="INR", source="test")
        assert out.outcome == "amount_mismatch"
    async with session_scope() as s:
        p = (await s.execute(select(Payment).where(Payment.public_id == pid))).scalar_one()
        assert p.status == "review"
        out = await settle_payment(s, payment_public_id=pid, gateway="razorpay", gateway_ref=p.provider_ref,
                                   amount_paise=50_000, currency="USD", source="test")
        assert out.outcome == "amount_mismatch"
    assert await _balance(1001) == 0


async def test_wrong_gateway_or_reference_never_credits(db):
    async with session_scope() as s:
        user = await _make_user(s)
        payment = await create_deposit(s, user, 50_000, settings=get_settings(), gateway=FakeGateway())
        pid = payment.public_id
    async with session_scope() as s:
        a = await settle_payment(s, payment_public_id=pid, gateway="razorpay", gateway_ref="plink_OTHER99999",
                                 amount_paise=50_000, currency="INR", source="test")
        b = await settle_payment(s, payment_public_id=pid, gateway="stripe", gateway_ref=None,
                                 amount_paise=50_000, currency="INR", source="test")
        c = await settle_payment(s, payment_public_id="PAY-NOPE0000", gateway="razorpay", gateway_ref=None,
                                 amount_paise=50_000, currency="INR", source="test")
    assert (a.outcome, b.outcome, c.outcome) == ("ref_mismatch", "gateway_mismatch", "unknown_payment")
    assert await _balance(1001) == 0


async def test_deposit_limits_terms_and_ban(db):
    async with session_scope() as s:
        user = await _make_user(s)
        with pytest.raises(DepositError):
            await create_deposit(s, user, 100, settings=get_settings(), gateway=FakeGateway())  # below minimum
        with pytest.raises(DepositError):
            await create_deposit(s, user, 10**9, settings=get_settings(), gateway=FakeGateway())
        no_terms = await _make_user(s, uid=1002, terms=False)
        with pytest.raises(DepositError):
            await create_deposit(s, no_terms, 50_000, settings=get_settings(), gateway=FakeGateway())
        banned = await _make_user(s, uid=1003)
        banned.is_banned = True
        with pytest.raises(DepositError):
            await create_deposit(s, banned, 50_000, settings=get_settings(), gateway=FakeGateway())


async def test_double_tap_returns_same_invoice(db):
    async with session_scope() as s:
        user = await _make_user(s)
        a = await create_deposit(s, user, 50_000, settings=get_settings(), gateway=FakeGateway(), idempotency_key="k1")
        b = await create_deposit(s, user, 50_000, settings=get_settings(), gateway=FakeGateway(), idempotency_key="k1")
        assert a.public_id == b.public_id


async def test_wallet_cannot_overspend(db):
    async with session_scope() as s:
        user = await _make_user(s)
        await credit(s, user, 1_000, "seed", idempotency_key="seed-1")
        await reserve(s, user, 600, "r1", idempotency_key="r-1")
        with pytest.raises(InsufficientFunds):
            await reserve(s, user, 600, "r2", idempotency_key="r-2")  # only 400 available
        assert int(user.available_paise) == 400
