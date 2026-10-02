"""Order engine.

Steps:
  1. Validate kill switches, user, service, quantity, extra fields
  2. Server-side quote (ignore any client price)
  3. Idempotency lookup
  4. Snapshot the service JSON
  5. Reserve wallet funds
  6. Persist order as awaiting_provider
  7. Submit to provider
  8. Capture funds on success, or release + mark failed
  9. Status sync / refill / cancel as follow-up actions
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.adapters.base import ProviderError
from app.catalog import (
    adapter_for,
    assert_orders_open,
    assert_refills_open,
    get_service,
)
from app.models import (
    ACTIVE_STATUSES,
    CANCELABLE_STATUSES,
    ORDER_STATUS_LABELS,
    REVIEW_STATUS,
    REFILLABLE_STATUSES,
    TERMINAL_STATUSES,
    Coupon,
    CouponRedemption,
    IdempotencyRecord,
    LedgerEntry,
    Order,
    OrderEvent,
    Reseller,
    ResellerOrder,
    Service,
    User,
)
from app.pricing import PriceQuote, quote_order
from app.audit import audit
from app.notify import notify_admins
from app.security import new_idempotency_key, new_public_id, utcnow
from app.wallet import InsufficientFunds, capture, credit, lock_user, release, reserve

log = logging.getLogger("falaron.orders")

_LINK_RE = re.compile(r"^https?://", re.IGNORECASE)

# Derived (no client key) idempotency keys are bucketed so a double-tap is deduplicated but a
# genuine repeat order a minute later is not swallowed.
IDEMPOTENCY_BUCKET_SECONDS = 45


class OrderError(Exception):
    pass


def status_label(status: str) -> str:
    return ORDER_STATUS_LABELS.get(status, status.replace("_", " ").title())


def validate_link(link: str) -> str:
    cleaned = (link or "").strip()
    if not cleaned:
        raise OrderError("A target link is required")
    if len(cleaned) > 1024:
        raise OrderError("Link is too long")
    if not _LINK_RE.match(cleaned):
        # Allow @username and t.me style without a scheme by adding https
        if cleaned.startswith("@"):
            return cleaned
        if " " in cleaned:
            raise OrderError("Link must not contain spaces")
        if "." in cleaned:
            return cleaned
        raise OrderError("Enter a valid URL or @username")
    return cleaned


def validate_extra(service: Service, extra: dict[str, Any] | None) -> dict[str, Any]:
    extra = dict(extra or {})
    stype = (service.service_type or "default").lower()
    if stype in {"comments", "custom_comments"}:
        comments = str(extra.get("comments") or "").strip()
        if not comments:
            raise OrderError("This service requires comments (one per line)")
        lines = [line.strip() for line in comments.splitlines() if line.strip()]
        extra["comments"] = "\n".join(lines)
        extra["comment_count"] = len(lines)
    if stype in {"mentions", "comment_mentions"}:
        mentions = str(extra.get("mentions") or extra.get("usernames") or "").strip()
        if not mentions:
            raise OrderError("This service requires usernames to mention")
        names = [n.strip().lstrip("@") for n in re.split(r"[\s,]+", mentions) if n.strip()]
        extra["mentions"] = " ".join(f"@{n}" for n in names)
        extra["usernames"] = "\n".join(names)
    return extra


def snapshot_service(service: Service, quote: PriceQuote) -> dict[str, Any]:
    return {
        "id": service.id,
        "name": service.name,
        "category_id": service.category_id,
        "provider_id": service.provider_id,
        "provider_service_id": service.provider_service_id,
        "service_type": service.service_type,
        "min_qty": service.min_qty,
        "max_qty": service.max_qty,
        "rate_per_1000_paise": service.rate_per_1000_paise,
        "sell_per_1000_paise": quote.sell_per_1000_paise,
        "pricing_layer": quote.layer,
        "markup_percent": quote.markup_percent,
        "is_refillable": service.is_refillable,
        "is_cancelable": service.is_cancelable,
        "average_time": service.average_time,
        "quoted_at": utcnow().isoformat(),
        "reseller_discount_paise": quote.reseller_discount_paise,
        "retail_per_1000_paise": quote.retail_per_1000_paise,
    }


async def _record_event(
    session: AsyncSession, order: Order, to_status: str, note: str | None = None
) -> None:
    event = OrderEvent(
        order_id=order.id,
        from_status=order.status,
        to_status=to_status,
        note=note,
        created_at=utcnow(),
    )
    session.add(event)
    order.status = to_status
    order.updated_at = utcnow()
    await session.flush()


async def _load_idempotent_order(session: AsyncSession, key: str) -> Order | None:
    rec = await session.get(IdempotencyRecord, key)
    if rec is None:
        return (
            await session.execute(select(Order).where(Order.idempotency_key == key))
        ).scalar_one_or_none()
    if rec.response_json:
        try:
            data = json.loads(rec.response_json)
            oid = data.get("order_id")
            if oid:
                return await session.get(Order, int(oid))
        except (ValueError, TypeError, json.JSONDecodeError):
            pass
    return (
        await session.execute(select(Order).where(Order.idempotency_key == key))
    ).scalar_one_or_none()


async def _release_coupon_use(session: AsyncSession, order: Order) -> None:
    """A coupon is only consumed by an order that was actually accepted."""
    if not order.coupon_code:
        return
    redemption = (
        await session.execute(
            select(CouponRedemption).where(
                CouponRedemption.coupon_code == order.coupon_code,
                CouponRedemption.order_id == order.id,
            )
        )
    ).scalar_one_or_none()
    if redemption is not None:
        await session.delete(redemption)
        coupon = await session.get(Coupon, order.coupon_code)
        if coupon is not None and int(coupon.used_count) > 0:
            coupon.used_count = int(coupon.used_count) - 1


async def _fail_and_release(
    session: AsyncSession, user: User, order: Order, charge_paise: int, reason: str, tag: str
) -> None:
    await release(
        session,
        user,
        charge_paise,
        reason=f"Release {order.public_id} ({tag})",
        idempotency_key=new_idempotency_key("release", order.public_id, tag),
        reference_type="order",
        reference_id=order.public_id,
    )
    order.fail_reason = reason[:255]
    await _release_coupon_use(session, order)
    await _record_event(session, order, "failed", reason[:255])


async def refunded_so_far(session: AsyncSession, order: Order) -> int:
    """Total already credited back to the customer for this order (from the immutable ledger)."""
    total = (
        await session.execute(
            select(func.coalesce(func.sum(LedgerEntry.amount_paise), 0)).where(
                LedgerEntry.reference_type == "order",
                LedgerEntry.reference_id == order.public_id,
                LedgerEntry.entry_type == "credit",
                LedgerEntry.user_id == order.user_id,
            )
        )
    ).scalar_one()
    return int(total or 0)


async def place_order(
    session: AsyncSession,
    user: User,
    *,
    service_id: str,
    link: str,
    quantity: int,
    extra: dict[str, Any] | None = None,
    coupon_code: str | None = None,
    idempotency_key: str | None = None,
) -> Order:
    if user.is_banned:
        raise OrderError("This account is suspended")
    if user.terms_accepted_at is None:
        raise OrderError("Please accept the Terms of Service and Privacy Policy first")
    await assert_orders_open(session)

    service = await get_service(session, service_id)
    if service is None or not service.is_active:
        raise OrderError("Service is not available")
    if not service.provider or not service.provider.is_active:
        raise OrderError("Provider is offline")

    link = validate_link(link)
    extra = validate_extra(service, extra)
    quote = await quote_order(
        session,
        service_id=service.id,
        quantity=quantity,
        user_id=user.telegram_id,
        coupon_code=coupon_code,
    )
    if quote.charge_paise <= 0:
        raise OrderError("Quoted charge is invalid")

    if idempotency_key:
        # Client keys are namespaced by user: one account can never replay or read another's order.
        key = new_idempotency_key("order-key", str(user.telegram_id), str(idempotency_key)[:128])
    else:
        bucket = int(utcnow().timestamp() // IDEMPOTENCY_BUCKET_SECONDS)
        key = new_idempotency_key(
            "order",
            str(user.telegram_id),
            service.id,
            link,
            str(quantity),
            json.dumps(extra, sort_keys=True, separators=(",", ":")),
            quote.coupon_code or "",
            str(bucket),
        )
    await lock_user(session, user)  # serialise this user's orders; also re-reads balance under the lock
    existing = await _load_idempotent_order(session, key)
    if existing is not None:
        if existing.status != "failed":
            return existing
        key = new_idempotency_key(key, "retry", utcnow().isoformat())

    if int(user.available_paise) < quote.charge_paise:
        raise InsufficientFunds(
            f"Need {quote.charge_paise} paise, available {int(user.available_paise)}"
        )

    snap = snapshot_service(service, quote)
    order = Order(
        public_id=new_public_id("FL", 6),
        user_id=user.telegram_id,
        service_id=service.id,
        provider_id=service.provider_id,
        status="pending",
        link=link,
        quantity=int(quantity),
        extra_json=json.dumps(extra, separators=(",", ":")) if extra else None,
        charge_paise=quote.charge_paise,
        cost_paise=quote.cost_paise,
        remains=int(quantity),
        service_snapshot_json=json.dumps(snap, separators=(",", ":")),
        idempotency_key=key,
        coupon_code=quote.coupon_code,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    session.add(order)
    await session.flush()

    session.add(
        IdempotencyRecord(
            key=key,
            scope="order",
            response_json=json.dumps({"order_id": order.id, "public_id": order.public_id}),
            created_at=utcnow(),
        )
    )

    reseller = await session.get(Reseller, user.telegram_id)
    if reseller is not None and reseller.status == "active":
        retail = scale_retail(quote)
        session.add(
            ResellerOrder(
                reseller_id=user.telegram_id,
                order_id=order.id,
                charge_paise=quote.charge_paise,
                retail_paise=retail,
                created_at=utcnow(),
            )
        )

    await reserve(
        session,
        user,
        quote.charge_paise,
        reason=f"Reserve for {order.public_id}",
        idempotency_key=new_idempotency_key("reserve", order.public_id),
        reference_type="order",
        reference_id=order.public_id,
    )
    await _record_event(session, order, "awaiting_provider", "Funds reserved")

    if quote.coupon_code:
        coupon = await session.get(Coupon, quote.coupon_code)
        if coupon is not None:
            coupon.used_count = int(coupon.used_count) + 1
        session.add(
            CouponRedemption(
                coupon_code=quote.coupon_code,
                user_id=user.telegram_id,
                order_id=order.id,
                created_at=utcnow(),
            )
        )

    try:
        adapter = await adapter_for(session, service.provider_id)
        placed = await adapter.create_order(
            service.provider_service_id, link, int(quantity), extra
        )
    except ProviderError as exc:
        if getattr(exc, "retryable", False):
            # Timeout / 5xx: the provider may or may not have accepted the order. Releasing the
            # funds now could give the customer a free delivery, so hold them and escalate.
            order.fail_reason = "Waiting for provider confirmation"
            await _record_event(session, order, REVIEW_STATUS, str(exc)[:255])
            await audit(session, actor="system", action="order.review", target=order.public_id,
                        new={"error": str(exc)[:200]}, reason="ambiguous provider response")
            log.warning("order %s held for review: %s", order.public_id, exc)
            await notify_admins(
                f"⚠️ Order <b>{order.public_id}</b> needs review: provider answer unknown "
                f"({str(exc)[:120]}). Funds are held. Check the provider panel, then run "
                f"<code>python -m app.cli resolve-order {order.public_id} --release</code> "
                f"or <code>--attach PROVIDER_ORDER_ID</code>."
            )
            return order
        await _fail_and_release(session, user, order, quote.charge_paise, str(exc)[:255], "provider")
        log.warning("order %s provider error: %s", order.public_id, exc)
        return order
    except Exception as exc:  # noqa: BLE001
        await _fail_and_release(
            session, user, order, quote.charge_paise, "Internal error submitting to provider", "internal"
        )
        log.exception("order %s unexpected submit error: %s", order.public_id, exc)
        return order

    order.provider_order_id = placed.external_id
    await capture(
        session,
        user,
        quote.charge_paise,
        reason=f"Charge {order.public_id}",
        idempotency_key=new_idempotency_key("capture", order.public_id),
        reference_type="order",
        reference_id=order.public_id,
    )
    await _record_event(session, order, "processing", f"Provider order {placed.external_id}")
    return order


def scale_retail(quote: PriceQuote) -> int:
    return max(quote.charge_paise + quote.reseller_discount_paise, quote.charge_paise)


async def get_order_for_user(
    session: AsyncSession, public_id: str, user_id: int | None = None
) -> Order | None:
    stmt = (
        select(Order)
        .options(selectinload(Order.service), selectinload(Order.events))
        .where(Order.public_id == public_id)
    )
    if user_id is not None:
        stmt = stmt.where(Order.user_id == user_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def list_orders(
    session: AsyncSession,
    user_id: int,
    *,
    limit: int = 10,
    offset: int = 0,
    status: str | None = None,
) -> list[Order]:
    stmt = (
        select(Order)
        .options(selectinload(Order.service))
        .where(Order.user_id == user_id)
        .order_by(Order.id.desc())
        .offset(offset)
        .limit(limit)
    )
    if status:
        stmt = stmt.where(Order.status == status)
    return list((await session.execute(stmt)).scalars().all())


async def sync_order_status(session: AsyncSession, order: Order) -> Order:
    if order.status in TERMINAL_STATUSES or not order.provider_order_id:
        return order
    try:
        adapter = await adapter_for(session, order.provider_id)
        status = await adapter.get_order_status(order.provider_order_id)
    except ProviderError as exc:
        log.warning("sync %s failed: %s", order.public_id, exc)
        order.last_synced_at = utcnow()
        return order

    mapped = status.status
    if mapped in {"cancelled"}:
        mapped = "canceled"
    changed = False
    if status.start_count is not None and order.start_count != status.start_count:
        order.start_count = status.start_count
        changed = True
    if status.remains is not None and order.remains != status.remains:
        order.remains = status.remains
        changed = True
    if mapped != order.status:
        await _record_event(session, order, mapped, "provider sync")
        changed = True
        if mapped == "partial" and status.remains is not None:
            await _maybe_partial_refund(session, order, status.remains)
        if mapped in {"canceled", "refunded", "failed"} and order.status != "refunded":
            # If provider canceled after we captured, refund remaining value.
            await _refund_if_needed(session, order, mapped)
        if mapped == "completed":
            from app.referrals import credit_order_commission

            await credit_order_commission(session, order)
    if changed:
        order.updated_at = utcnow()
    order.last_synced_at = utcnow()
    return order


async def _maybe_partial_refund(session: AsyncSession, order: Order, remains: int) -> None:
    if remains <= 0 or order.quantity <= 0:
        return
    already = (
        await session.execute(
            select(IdempotencyRecord).where(
                IdempotencyRecord.key == new_idempotency_key("partial", order.public_id)
            )
        )
    ).scalar_one_or_none()
    if already is not None:
        return
    fraction = min(1.0, max(0.0, remains / float(order.quantity)))
    refund = int(round(order.charge_paise * fraction))
    refund = min(refund, max(0, int(order.charge_paise) - await refunded_so_far(session, order)))
    if refund <= 0:
        return
    user = await session.get(User, order.user_id)
    if user is None:
        return
    await credit(
        session,
        user,
        refund,
        reason=f"Partial refund {order.public_id}",
        idempotency_key=new_idempotency_key("partial", order.public_id),
        reference_type="order",
        reference_id=order.public_id,
    )
    session.add(
        IdempotencyRecord(
            key=new_idempotency_key("partial", order.public_id),
            scope="refund",
            response_json=json.dumps({"refund_paise": refund}),
            created_at=utcnow(),
        )
    )


async def _refund_if_needed(session: AsyncSession, order: Order, mapped: str) -> None:
    key = new_idempotency_key("refund", order.public_id, mapped)
    existing = await session.get(IdempotencyRecord, key)
    if existing is not None:
        return
    user = await session.get(User, order.user_id)
    if user is None or order.charge_paise <= 0:
        return
    # A partial refund may already have been paid: only return what is still owed.
    owed = max(0, int(order.charge_paise) - await refunded_so_far(session, order))
    if owed <= 0:
        return
    await credit(
        session,
        user,
        owed,
        reason=f"Refund {order.public_id} ({mapped})",
        idempotency_key=key,
        reference_type="order",
        reference_id=order.public_id,
    )
    session.add(
        IdempotencyRecord(
            key=key,
            scope="refund",
            response_json=json.dumps({"refund_paise": owed}),
            created_at=utcnow(),
        )
    )
    if mapped != "refunded":
        await _record_event(session, order, "refunded", f"auto-refund after {mapped}")


async def request_refill(session: AsyncSession, order: Order, user: User) -> Order:
    await assert_refills_open(session)
    if order.user_id != user.telegram_id and user.role not in {"admin", "owner"}:
        raise OrderError("Not your order")
    snap = json.loads(order.service_snapshot_json or "{}")
    refillable = bool(snap.get("is_refillable", True))
    if not refillable:
        raise OrderError("This service does not support refill")
    if order.status not in REFILLABLE_STATUSES:
        raise OrderError("Refill is only available after completion")
    if not order.provider_order_id:
        raise OrderError("No provider order to refill")
    adapter = await adapter_for(session, order.provider_id)
    try:
        refill_id = await adapter.refill(order.provider_order_id)
    except ProviderError as exc:
        raise OrderError(str(exc)) from exc
    order.refill_count = int(order.refill_count or 0) + 1
    extra = order.extra()
    extra["last_refill_id"] = refill_id
    order.extra_json = json.dumps(extra, separators=(",", ":"))
    await _record_event(session, order, "refilling", f"refill {refill_id}")
    return order


async def request_cancel(session: AsyncSession, order: Order, user: User) -> Order:
    if order.user_id != user.telegram_id and user.role not in {"admin", "owner"}:
        raise OrderError("Not your order")
    snap = json.loads(order.service_snapshot_json or "{}")
    if not bool(snap.get("is_cancelable", True)):
        raise OrderError("This service cannot be canceled")
    if order.status not in CANCELABLE_STATUSES:
        raise OrderError("This order can no longer be canceled")
    if order.provider_order_id:
        adapter = await adapter_for(session, order.provider_id)
        try:
            await adapter.cancel([order.provider_order_id])
        except ProviderError as exc:
            raise OrderError(str(exc)) from exc
    user_row = await session.get(User, order.user_id)
    if user_row is not None and order.charge_paise > 0 and order.status != REVIEW_STATUS:
        # If still reserved (awaiting_provider) release; otherwise credit back.
        if order.status in {"pending", "awaiting_provider"}:
            from app.wallet import release as wallet_release

            await wallet_release(
                session,
                user_row,
                order.charge_paise,
                reason=f"Release {order.public_id} (cancel)",
                idempotency_key=new_idempotency_key("release", order.public_id, "cancel"),
                reference_type="order",
                reference_id=order.public_id,
            )
        else:
            owed = max(0, int(order.charge_paise) - await refunded_so_far(session, order))
            if owed <= 0:
                await _record_event(session, order, "canceled", "user cancel")
                return order
            await credit(
                session,
                user_row,
                owed,
                reason=f"Refund {order.public_id} (cancel)",
                idempotency_key=new_idempotency_key("refund", order.public_id, "cancel"),
                reference_type="order",
                reference_id=order.public_id,
            )
    await _record_event(session, order, "canceled", "user cancel")
    return order


async def list_open_orders(session: AsyncSession, *, limit: int = 50) -> list[Order]:
    result = await session.execute(
        select(Order)
        .where(Order.status.in_(tuple(ACTIVE_STATUSES)))
        .where(Order.provider_order_id.is_not(None))
        .order_by(Order.updated_at.asc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def list_review_orders(session: AsyncSession, *, limit: int = 50) -> list[Order]:
    result = await session.execute(
        select(Order).where(Order.status == REVIEW_STATUS).order_by(Order.id.asc()).limit(limit)
    )
    return list(result.scalars().all())


async def resolve_review_order(
    session: AsyncSession,
    order: Order,
    *,
    release_funds: bool = False,
    attach_provider_order_id: str | None = None,
    actor: str = "cli",
) -> Order:
    """Close out an order whose provider outcome was unknown.

    release_funds=True        provider never received it -> refund the customer, mark failed.
    attach_provider_order_id  provider did receive it     -> capture funds, continue as processing.
    """
    if order.status != REVIEW_STATUS:
        raise OrderError("Order is not under review")
    if bool(release_funds) == bool(attach_provider_order_id):
        raise OrderError("Choose exactly one: release the funds or attach a provider order id")
    user = await session.get(User, order.user_id)
    if user is None:
        raise OrderError("Order owner not found")
    if release_funds:
        await _fail_and_release(
            session, user, order, int(order.charge_paise), "Not accepted by provider", "review"
        )
        await audit(session, actor=actor, action="order.review.release", target=order.public_id)
        return order
    order.provider_order_id = str(attach_provider_order_id)[:64]
    await capture(
        session,
        user,
        int(order.charge_paise),
        reason=f"Charge {order.public_id}",
        idempotency_key=new_idempotency_key("capture", order.public_id),
        reference_type="order",
        reference_id=order.public_id,
    )
    order.fail_reason = None
    await _record_event(session, order, "processing", f"Provider order {order.provider_order_id} (manual)")
    await audit(session, actor=actor, action="order.review.attach", target=order.public_id,
                new={"provider_order_id": order.provider_order_id})
    return order


async def flag_stuck_orders(session: AsyncSession, *, older_than_seconds: int = 600, limit: int = 50) -> list[Order]:
    """Orders that reserved funds but never reached the provider call's outcome (process crash,
    deploy mid-request). Funds stay held; an operator resolves them with resolve_review_order."""
    from datetime import timedelta

    cutoff = utcnow() - timedelta(seconds=older_than_seconds)
    rows = (
        await session.execute(
            select(Order)
            .where(
                Order.status.in_(("pending", "awaiting_provider")),
                Order.provider_order_id.is_(None),
                Order.updated_at <= cutoff,
            )
            .order_by(Order.id.asc())
            .limit(limit)
        )
    ).scalars().all()
    for order in rows:
        order.fail_reason = "Waiting for provider confirmation"
        await _record_event(session, order, REVIEW_STATUS, "stuck after reserve; needs operator review")
        await audit(session, actor="system", action="order.review", target=order.public_id,
                    reason="stuck before provider outcome")
    return list(rows)
