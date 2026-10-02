"""Deposit lifecycle: create -> (gateway) -> verified settle -> wallet credit.

Rules (blueprint sections 17-19):
  * A wallet is credited only from a server-verified event or a gateway status lookup, never from
    anything the browser or bot user says.
  * Amount, currency, reference and gateway must all match the invoice we created.
  * Webhook redelivery is harmless: (gateway, event_id) is unique and the ledger key is per payment.
  * Anything odd (amount mismatch, refund, dispute) is held for a human, not auto-resolved.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Mapping

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import audit
from app.catalog import assert_payments_open, get_setting
from app.config import Settings, get_settings
from app.models import Payment, PaymentEvent, User
from app.payments.base import GatewayError, GatewayEvent, PaymentGateway
from app.security import new_idempotency_key, new_public_id, utcnow
from app.wallet import credit

log = logging.getLogger("falaron.payments")

PENDING, PAID, EXPIRED, FAILED, REVIEW = "pending", "paid", "expired", "failed", "review"
IDEMPOTENCY_BUCKET_SECONDS = 45

_gateway_cache: PaymentGateway | None = None
_gateway_cache_key: tuple | None = None


class DepositError(Exception):
    """A user-facing deposit problem (message is safe to show)."""


@dataclass
class SettleOutcome:
    outcome: str  # credited | already_paid | amount_mismatch | unknown_payment | gateway_mismatch | ref_mismatch
    payment_public_id: str | None = None
    user_id: int | None = None
    amount_paise: int | None = None


@dataclass
class WebhookOutcome:
    outcome: str  # duplicate | credited | already_paid | expired | ignored | manual_review | ...
    payment_public_id: str | None = None
    user_id: int | None = None
    amount_paise: int | None = None


def get_gateway(settings: Settings | None = None) -> PaymentGateway | None:
    """The configured live gateway, or None for mock/manual modes."""
    global _gateway_cache, _gateway_cache_key
    settings = settings or get_settings()
    if settings.payment_gateway != "razorpay":
        return None
    key = (
        settings.razorpay_key_id,
        settings.razorpay_secret_value(),
        settings.razorpay_webhook_secret_value(),
        settings.razorpay_base_url,
    )
    if _gateway_cache is not None and _gateway_cache_key == key:
        return _gateway_cache
    from app.payments.razorpay import RazorpayGateway

    if not (key[0] and key[1] and key[2]):
        raise DepositError("Online payments are not configured")
    _gateway_cache = RazorpayGateway(key[0], key[1], key[2], base_url=key[3])
    _gateway_cache_key = key
    return _gateway_cache


def reset_gateway_cache() -> None:
    global _gateway_cache, _gateway_cache_key
    _gateway_cache = None
    _gateway_cache_key = None


# --------------------------------------------------------------------------- create
async def create_deposit(
    session: AsyncSession,
    user: User,
    amount_paise: int,
    *,
    settings: Settings | None = None,
    idempotency_key: str | None = None,
    gateway: PaymentGateway | None = None,
) -> Payment:
    settings = settings or get_settings()
    if user.is_banned:
        raise DepositError("This account is suspended")
    if user.terms_accepted_at is None:
        raise DepositError("Please accept the Terms of Service and Privacy Policy first")
    try:
        await assert_payments_open(session)
    except PermissionError as exc:
        raise DepositError(str(exc)) from exc
    amount_paise = int(amount_paise)
    if amount_paise < settings.min_deposit_paise or amount_paise > settings.max_deposit_paise:
        lo, hi = settings.min_deposit_paise // 100, settings.max_deposit_paise // 100
        raise DepositError(f"Deposit must be between ₹{lo:,} and ₹{hi:,}")

    if idempotency_key:
        key = new_idempotency_key("pay-key", str(user.telegram_id), str(idempotency_key)[:128])
    else:
        bucket = int(utcnow().timestamp() // IDEMPOTENCY_BUCKET_SECONDS)
        key = new_idempotency_key("pay", str(user.telegram_id), str(amount_paise), str(bucket))
    existing = (
        await session.execute(select(Payment).where(Payment.idempotency_key == key))
    ).scalar_one_or_none()
    if existing is not None and existing.user_id == user.telegram_id:
        return existing

    method = settings.payment_gateway
    payment = Payment(
        public_id=new_public_id("PAY"),
        user_id=user.telegram_id,
        amount_paise=amount_paise,
        method=method,
        status=PENDING,
        currency=settings.currency_code,
        idempotency_key=key,
        created_at=utcnow(),
    )
    session.add(payment)
    await session.flush()

    if method == "razorpay":
        gw = gateway or get_gateway(settings)
        if gw is None:
            raise DepositError("Online payments are not configured")
        callback = f"{settings.public_base_url}/pay/return" if settings.public_base_url else None
        try:
            checkout = await gw.create_checkout(
                reference_id=payment.public_id,
                amount_paise=amount_paise,
                currency=settings.currency_code,
                description="Wallet top-up",
                notes={"telegram_id": str(user.telegram_id), "payment": payment.public_id},
                callback_url=callback,
            )
        except GatewayError as exc:
            log.warning("checkout creation failed for %s: %s", payment.public_id, exc)
            await session.delete(payment)  # no checkout exists: do not leave a dead invoice behind
            await session.flush()
            raise DepositError("The payment provider is unavailable. Please try again shortly.") from exc
        payment.provider_ref = checkout.gateway_ref
        payment.checkout_url = checkout.url
        await session.flush()
    return payment


# --------------------------------------------------------------------------- settle
async def settle_payment(
    session: AsyncSession,
    *,
    payment_public_id: str,
    gateway: str,
    gateway_ref: str | None,
    amount_paise: int | None,
    currency: str | None,
    source: str,
    actor: str = "system",
) -> SettleOutcome:
    """Credit the wallet for a verified payment. Idempotent and mismatch-safe."""
    payment = (
        await session.execute(
            select(Payment).where(Payment.public_id == payment_public_id).with_for_update()
        )
    ).scalar_one_or_none()
    if payment is None:
        return SettleOutcome("unknown_payment", payment_public_id)
    base = dict(payment_public_id=payment.public_id, user_id=payment.user_id)
    if payment.method != gateway and source != "manual":
        return SettleOutcome("gateway_mismatch", **base)
    if payment.provider_ref and gateway_ref and payment.provider_ref != gateway_ref:
        return SettleOutcome("ref_mismatch", **base)
    if payment.status == PAID:
        return SettleOutcome("already_paid", amount_paise=payment.amount_paise, **base)

    cur = (currency or payment.currency or "").upper()
    if amount_paise is None or int(amount_paise) != int(payment.amount_paise) or cur != payment.currency.upper():
        payment.status = REVIEW
        payment.failure_reason = (
            f"mismatch: expected {payment.amount_paise} {payment.currency}, got {amount_paise} {cur}"
        )[:255]
        await audit(session, actor=actor, action="payment.mismatch", target=payment.public_id,
                    old={"amount": payment.amount_paise, "currency": payment.currency},
                    new={"amount": amount_paise, "currency": cur}, reason=source)
        return SettleOutcome("amount_mismatch", amount_paise=amount_paise, **base)

    user = await session.get(User, payment.user_id)
    if user is None:
        payment.status = REVIEW
        payment.failure_reason = "owner not found"
        return SettleOutcome("unknown_payment", **base)

    await credit(
        session,
        user,
        payment.amount_paise,
        reason=f"Deposit {payment.public_id}",
        idempotency_key=new_idempotency_key("deposit", payment.public_id),
        reference_type="payment",
        reference_id=payment.public_id,
        meta={"gateway": gateway, "source": source},
    )
    payment.status = PAID
    payment.paid_at = utcnow()
    payment.failure_reason = None
    if gateway_ref and not payment.provider_ref:
        payment.provider_ref = gateway_ref[:64]
    from app.referrals import credit_deposit_commission

    await credit_deposit_commission(session, user, payment)
    await audit(session, actor=actor, action="payment.credited", target=payment.public_id,
                new={"amount": payment.amount_paise, "source": source})
    return SettleOutcome("credited", amount_paise=payment.amount_paise, **base)


# --------------------------------------------------------------------------- webhook
async def handle_webhook_event(session: AsyncSession, gateway: str, event: GatewayEvent) -> WebhookOutcome:
    """Process one signature-verified gateway event exactly once."""
    row = PaymentEvent(
        gateway=gateway,
        event_id=event.event_id,
        event_type=event.event_type,
        payment_public_id=event.reference,
        amount_paise=event.amount_paise,
        currency=event.currency,
        outcome="processing",
        created_at=utcnow(),
    )
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError:
        return WebhookOutcome("duplicate", event.reference)

    outcome: WebhookOutcome
    if event.kind == "paid" and event.reference:
        settled = await settle_payment(
            session,
            payment_public_id=event.reference,
            gateway=gateway,
            gateway_ref=event.gateway_ref,
            amount_paise=event.amount_paise,
            currency=event.currency,
            source="webhook",
        )
        outcome = WebhookOutcome(settled.outcome, settled.payment_public_id, settled.user_id, settled.amount_paise)
    elif event.kind == "expired" and event.reference:
        payment = (
            await session.execute(
                select(Payment).where(Payment.public_id == event.reference).with_for_update()
            )
        ).scalar_one_or_none()
        if payment is not None and payment.status == PENDING:
            payment.status = EXPIRED
            outcome = WebhookOutcome("expired", payment.public_id, payment.user_id)
        else:
            outcome = WebhookOutcome("ignored", event.reference)
    elif event.kind == "attention":
        await audit(session, actor="system", action="payment.attention", target=event.reference,
                    new={"type": event.event_type, "detail": event.detail})
        outcome = WebhookOutcome("manual_review", event.reference, amount_paise=event.amount_paise)
    else:
        outcome = WebhookOutcome("ignored", event.reference)

    row.outcome = outcome.outcome[:32]
    row.detail = (event.detail or None) and event.detail[:255]
    return outcome


# --------------------------------------------------------------------------- reconcile
async def reconcile_pending(
    session: AsyncSession,
    gateway: PaymentGateway,
    *,
    min_age_seconds: int = 120,
    max_age_hours: int = 72,
    limit: int = 50,
) -> list[WebhookOutcome]:
    """Ask the gateway about still-pending invoices so a lost webhook cannot lose money."""
    now = utcnow()
    rows = (
        await session.execute(
            select(Payment)
            .where(
                Payment.method == gateway.name,
                Payment.status.in_((PENDING, EXPIRED)),
                Payment.provider_ref.is_not(None),
                Payment.created_at <= now - timedelta(seconds=min_age_seconds),
                Payment.created_at >= now - timedelta(hours=max_age_hours),
            )
            .order_by(Payment.id.asc())
            .limit(limit)
        )
    ).scalars().all()
    results: list[WebhookOutcome] = []
    for payment in rows:
        try:
            status = await gateway.fetch_status(payment.provider_ref or "")
        except GatewayError as exc:
            log.warning("reconcile %s: %s", payment.public_id, exc)
            continue
        if status.state == "paid":
            settled = await settle_payment(
                session,
                payment_public_id=payment.public_id,
                gateway=gateway.name,
                gateway_ref=payment.provider_ref,
                amount_paise=status.amount_paise,
                currency=status.currency,
                source="reconcile",
            )
            results.append(
                WebhookOutcome(settled.outcome, settled.payment_public_id, settled.user_id, settled.amount_paise)
            )
        elif status.state == "expired" and payment.status == PENDING:
            payment.status = EXPIRED
            results.append(WebhookOutcome("expired", payment.public_id, payment.user_id))
    return results


async def manual_confirm(session: AsyncSession, payment_public_id: str, *, actor: str) -> SettleOutcome:
    """Operator confirms an off-platform payment (PAYMENT_GATEWAY=manual). Fully audited."""
    payment = (
        await session.execute(select(Payment).where(Payment.public_id == payment_public_id))
    ).scalar_one_or_none()
    if payment is None:
        return SettleOutcome("unknown_payment", payment_public_id)
    return await settle_payment(
        session,
        payment_public_id=payment.public_id,
        gateway=payment.method,
        gateway_ref=payment.provider_ref,
        amount_paise=payment.amount_paise,
        currency=payment.currency,
        source="manual",
        actor=actor,
    )


async def payment_instructions(session: AsyncSession) -> str:
    return await get_setting(session, "payment_instructions", "")


def describe_outcome(o: WebhookOutcome | SettleOutcome) -> str:
    return f"{o.outcome} {o.payment_public_id or ''}".strip()


async def check_payment(
    session: AsyncSession,
    payment_public_id: str,
    user_id: int,
    *,
    settings: Settings | None = None,
    gateway: PaymentGateway | None = None,
) -> Payment | None:
    """"I have paid" button: ask the gateway directly. Never trusts the user's claim.

    Returns the payment (with an up-to-date status), or None when it is not theirs.
    """
    settings = settings or get_settings()
    payment = (
        await session.execute(select(Payment).where(Payment.public_id == payment_public_id))
    ).scalar_one_or_none()
    if payment is None or payment.user_id != user_id:
        return None
    if payment.status != PAID and payment.method == "razorpay" and payment.provider_ref:
        gw = gateway or get_gateway(settings)
        if gw is not None:
            try:
                status = await gw.fetch_status(payment.provider_ref)
            except GatewayError as exc:
                log.warning("check_payment %s: %s", payment.public_id, exc)
            else:
                if status.state == "paid":
                    await settle_payment(
                        session,
                        payment_public_id=payment.public_id,
                        gateway=gw.name,
                        gateway_ref=payment.provider_ref,
                        amount_paise=status.amount_paise,
                        currency=status.currency,
                        source="user-check",
                    )
                elif status.state == "expired" and payment.status == PENDING:
                    payment.status = EXPIRED
    return payment
