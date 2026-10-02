"""Referral attribution, commission ledger, partner (reseller/promoter) helpers."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from hashlib import sha256

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    DEFAULT_RESELLER_DISCOUNT_BP,
    MIN_PAYOUT_PAISE,
    REFERRAL_DEPOSIT_COMMISSION_PERCENT,
    REFERRAL_ORDER_CAP,
    REFERRAL_ORDER_COMMISSION_PERCENT,
    ACTIVE_STATUSES,
    LedgerEntry,
    Order,
    Payment,
    Promoter,
    ReferralClick,
    Reseller,
    RoleApplication,
    User,
)
from app.pricing import paise_to_rupees_str
from app.security import new_idempotency_key, utcnow
from app.wallet import WalletError, credit, debit

log = logging.getLogger("falaron.referrals")

_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def make_referral_code(telegram_id: int) -> str:
    digest = sha256(f"falaron-ref:{telegram_id}".encode("utf-8")).digest()
    return "".join(_ALPHABET[b % len(_ALPHABET)] for b in digest[:6])


async def ensure_referral_code(session: AsyncSession, user: User) -> str:
    if user.referral_code:
        return user.referral_code
    code = make_referral_code(user.telegram_id)
    clash = (
        await session.execute(
            select(User).where(User.referral_code == code, User.telegram_id != user.telegram_id)
        )
    ).scalar_one_or_none()
    if clash is not None:
        code = f"{code}{user.telegram_id % 9}"
    user.referral_code = code
    user.updated_at = utcnow()
    await session.flush()
    return code


async def user_by_referral_code(session: AsyncSession, code: str) -> User | None:
    normalized = (code or "").strip().upper()
    if not normalized:
        return None
    return (
        await session.execute(select(User).where(User.referral_code == normalized))
    ).scalar_one_or_none()


async def record_start_hit(
    session: AsyncSession,
    *,
    code: str,
    visitor: User,
) -> User | None:
    """Attribute a /start deep-link. Returns the referrer if valid."""
    code = (code or "").strip().upper()
    if not code:
        return None
    referrer = await user_by_referral_code(session, code)
    is_self = referrer is not None and referrer.telegram_id == visitor.telegram_id
    if is_self:
        return None
    is_new = visitor.referred_by is None and referrer is not None
    if is_new and referrer is not None:
        visitor.referred_by = referrer.telegram_id
        visitor.updated_at = utcnow()
    session.add(
        ReferralClick(
            code=code[:16],
            referrer_id=None if referrer is None else referrer.telegram_id,
            visitor_id=visitor.telegram_id,
            is_signup=bool(is_new),
            created_at=utcnow(),
        )
    )
    await session.flush()
    return referrer


async def ensure_owner_partners(session: AsyncSession, user: User) -> None:
    """Owner (and role=owner) is always an active reseller + promoter."""
    if user.role not in {"owner", "admin"}:
        return
    res = await session.get(Reseller, user.telegram_id)
    if res is None:
        session.add(
            Reseller(
                user_id=user.telegram_id,
                tier="gold",
                discount_bp=DEFAULT_RESELLER_DISCOUNT_BP,
                status="active",
                notes="auto-owner",
                created_at=utcnow(),
                updated_at=utcnow(),
            )
        )
    else:
        res.status = "active"
        if not res.discount_bp:
            res.discount_bp = DEFAULT_RESELLER_DISCOUNT_BP
    pro = await session.get(Promoter, user.telegram_id)
    if pro is None:
        session.add(
            Promoter(
                user_id=user.telegram_id,
                status="active",
                notes="auto-owner",
                created_at=utcnow(),
                updated_at=utcnow(),
            )
        )
    else:
        pro.status = "active"
    await session.flush()


async def active_reseller(session: AsyncSession, user_id: int) -> Reseller | None:
    row = await session.get(Reseller, user_id)
    if row is None or row.status != "active":
        return None
    return row


async def active_promoter(session: AsyncSession, user_id: int) -> Promoter | None:
    row = await session.get(Promoter, user_id)
    if row is None or row.status != "active":
        return None
    return row


async def is_reseller_user(session: AsyncSession, user: User, owner_id: int) -> bool:
    if user.telegram_id == owner_id or user.role in {"owner", "admin"}:
        await ensure_owner_partners(session, user)
        return True
    if user.role == "reseller":
        return True
    row = await active_reseller(session, user.telegram_id)
    return row is not None


async def is_promoter_user(session: AsyncSession, user: User, owner_id: int) -> bool:
    if user.telegram_id == owner_id or user.role in {"owner", "admin"}:
        await ensure_owner_partners(session, user)
        return True
    if user.role == "promoter":
        return True
    row = await active_promoter(session, user.telegram_id)
    return row is not None


async def partner_flags(session: AsyncSession, user: User, owner_id: int) -> tuple[bool, bool]:
    return (
        await is_reseller_user(session, user, owner_id),
        await is_promoter_user(session, user, owner_id),
    )


def _commission_paise(amount: int, percent: float) -> int:
    if amount <= 0 or percent <= 0:
        return 0
    return int(amount * percent / 100.0)


async def credit_order_commission(session: AsyncSession, order: Order) -> LedgerEntry | None:
    """8% of a referred user's first 3 completed order amounts. Idempotent."""
    if order.status != "completed" or order.charge_paise <= 0:
        return None
    buyer = await session.get(User, order.user_id)
    if buyer is None or buyer.referred_by is None:
        return None
    completed_ids = list(
        (
            await session.execute(
                select(Order.id)
                .where(Order.user_id == buyer.telegram_id, Order.status == "completed")
                .order_by(Order.id.asc())
            )
        ).scalars().all()
    )
    if order.id not in completed_ids[:REFERRAL_ORDER_CAP]:
        return None
    referrer = await session.get(User, buyer.referred_by)
    if referrer is None or referrer.telegram_id == buyer.telegram_id:
        return None
    amount = _commission_paise(order.charge_paise, REFERRAL_ORDER_COMMISSION_PERCENT)
    if amount <= 0:
        return None
    key = new_idempotency_key("reford", order.public_id)
    entry = await credit(
        session,
        referrer,
        amount,
        reason=f"Referral 8% {order.public_id}",
        idempotency_key=key,
        reference_type="referral",
        reference_id=order.public_id,
        meta={"kind": "order", "buyer_id": buyer.telegram_id, "pct": REFERRAL_ORDER_COMMISSION_PERCENT},
    )
    log.info(
        "referral order commission referrer=%s order=%s amount=%s",
        referrer.telegram_id,
        order.public_id,
        amount,
    )
    return entry


async def credit_deposit_commission(
    session: AsyncSession, buyer: User, payment: Payment
) -> LedgerEntry | None:
    """5% of a referred user's first deposit. Idempotent per buyer."""
    if buyer.referred_by is None or payment.amount_paise <= 0:
        return None
    paid_count = (
        await session.execute(
            select(func.count())
            .select_from(Payment)
            .where(Payment.user_id == buyer.telegram_id, Payment.status == "paid")
        )
    ).scalar_one()
    if int(paid_count) > 1:
        return None
    referrer = await session.get(User, buyer.referred_by)
    if referrer is None or referrer.telegram_id == buyer.telegram_id:
        return None
    amount = _commission_paise(payment.amount_paise, REFERRAL_DEPOSIT_COMMISSION_PERCENT)
    if amount <= 0:
        return None
    key = new_idempotency_key("refdep", str(buyer.telegram_id))
    entry = await credit(
        session,
        referrer,
        amount,
        reason=f"Referral 5% first deposit {payment.public_id}",
        idempotency_key=key,
        reference_type="referral",
        reference_id=payment.public_id,
        meta={"kind": "deposit", "buyer_id": buyer.telegram_id, "pct": REFERRAL_DEPOSIT_COMMISSION_PERCENT},
    )
    log.info(
        "referral deposit commission referrer=%s payment=%s amount=%s",
        referrer.telegram_id,
        payment.public_id,
        amount,
    )
    return entry


@dataclass(frozen=True)
class ReferralSnapshot:
    code: str
    invited: int
    earnings_paise: int
    pending_paise: int
    converting: int


async def referral_snapshot(session: AsyncSession, user: User) -> ReferralSnapshot:
    code = await ensure_referral_code(session, user)
    invited = int(
        (
            await session.execute(
                select(func.count()).select_from(User).where(User.referred_by == user.telegram_id)
            )
        ).scalar_one()
    )
    earnings = int(
        (
            await session.execute(
                select(func.coalesce(func.sum(LedgerEntry.amount_paise), 0)).where(
                    LedgerEntry.user_id == user.telegram_id,
                    LedgerEntry.reference_type == "referral",
                    LedgerEntry.entry_type == "credit",
                )
            )
        ).scalar_one()
    )
    converting = int(
        (
            await session.execute(
                select(func.count(func.distinct(Order.user_id))).where(
                    Order.user_id.in_(
                        select(User.telegram_id).where(User.referred_by == user.telegram_id)
                    ),
                    Order.status == "completed",
                )
            )
        ).scalar_one()
    )
    downline_ids = list(
        (
            await session.execute(select(User.telegram_id).where(User.referred_by == user.telegram_id))
        ).scalars().all()
    )
    pending = 0
    if downline_ids:
        open_orders = list(
            (
                await session.execute(
                    select(Order).where(
                        Order.user_id.in_(downline_ids),
                        Order.status.in_(tuple(ACTIVE_STATUSES)),
                    )
                )
            ).scalars().all()
        )
        completed_by_user: dict[int, int] = {}
        for uid in downline_ids:
            completed_by_user[uid] = int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(Order)
                        .where(Order.user_id == uid, Order.status == "completed")
                    )
                ).scalar_one()
            )
        remaining_slots = {
            uid: max(0, REFERRAL_ORDER_CAP - completed_by_user.get(uid, 0)) for uid in downline_ids
        }
        used: dict[int, int] = {uid: 0 for uid in downline_ids}
        for order in sorted(open_orders, key=lambda o: o.id):
            if used[order.user_id] >= remaining_slots.get(order.user_id, 0):
                continue
            pending += _commission_paise(order.charge_paise, REFERRAL_ORDER_COMMISSION_PERCENT)
            used[order.user_id] += 1
    return ReferralSnapshot(
        code=code,
        invited=invited,
        earnings_paise=earnings,
        pending_paise=pending,
        converting=converting,
    )


@dataclass(frozen=True)
class PromoterSnapshot:
    clicks: int
    signups: int
    converting: int
    commission_paise: int
    available_paise: int
    code: str


async def promoter_snapshot(session: AsyncSession, user: User) -> PromoterSnapshot:
    code = await ensure_referral_code(session, user)
    ref = await referral_snapshot(session, user)
    clicks = int(
        (
            await session.execute(
                select(func.count()).select_from(ReferralClick).where(
                    (ReferralClick.referrer_id == user.telegram_id) | (ReferralClick.code == code)
                )
            )
        ).scalar_one()
    )
    signups = int(
        (
            await session.execute(
                select(func.count()).select_from(ReferralClick).where(
                    ReferralClick.referrer_id == user.telegram_id,
                    ReferralClick.is_signup.is_(True),
                )
            )
        ).scalar_one()
    )
    if signups < ref.invited:
        signups = ref.invited
    return PromoterSnapshot(
        clicks=clicks,
        signups=signups,
        converting=ref.converting,
        commission_paise=ref.earnings_paise,
        available_paise=int(user.available_paise),
        code=code,
    )


@dataclass(frozen=True)
class ResellerSnapshot:
    tier: str
    discount_bp: int
    orders: int
    gmv_paise: int
    wallet_paise: int
    downline: int
    status: str


async def reseller_snapshot(session: AsyncSession, user: User) -> ResellerSnapshot:
    row = await session.get(Reseller, user.telegram_id)
    tier = row.tier if row else "bronze"
    discount_bp = row.discount_bp if row else DEFAULT_RESELLER_DISCOUNT_BP
    status = row.status if row else "none"
    orders = int(
        (
            await session.execute(
                select(func.count()).select_from(Order).where(Order.user_id == user.telegram_id)
            )
        ).scalar_one()
    )
    gmv = int(
        (
            await session.execute(
                select(func.coalesce(func.sum(Order.charge_paise), 0)).where(
                    Order.user_id == user.telegram_id,
                    Order.status.notin_(("failed", "canceled", "cancelled")),
                )
            )
        ).scalar_one()
    )
    downline = int(
        (
            await session.execute(
                select(func.count()).select_from(User).where(User.referred_by == user.telegram_id)
            )
        ).scalar_one()
    )
    return ResellerSnapshot(
        tier=tier,
        discount_bp=discount_bp,
        orders=orders,
        gmv_paise=gmv,
        wallet_paise=int(user.available_paise),
        downline=downline,
        status=status,
    )


async def pending_application(
    session: AsyncSession, user_id: int, kind: str
) -> RoleApplication | None:
    return (
        await session.execute(
            select(RoleApplication)
            .where(
                RoleApplication.user_id == user_id,
                RoleApplication.kind == kind,
                RoleApplication.status == "pending",
            )
            .order_by(RoleApplication.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def submit_application(
    session: AsyncSession, user: User, kind: str, pitch: str
) -> RoleApplication:
    existing = await pending_application(session, user.telegram_id, kind)
    if existing is not None:
        existing.pitch = pitch[:2000]
        await session.flush()
        return existing
    row = RoleApplication(
        user_id=user.telegram_id,
        kind=kind,
        pitch=pitch[:2000],
        status="pending",
        created_at=utcnow(),
    )
    session.add(row)
    await session.flush()
    return row


async def approve_application(session: AsyncSession, application: RoleApplication) -> None:
    application.status = "approved"
    user = await session.get(User, application.user_id)
    if user is None:
        return
    if application.kind == "reseller":
        row = await session.get(Reseller, user.telegram_id)
        if row is None:
            session.add(
                Reseller(
                    user_id=user.telegram_id,
                    tier="bronze",
                    discount_bp=DEFAULT_RESELLER_DISCOUNT_BP,
                    status="active",
                    created_at=utcnow(),
                    updated_at=utcnow(),
                )
            )
        else:
            row.status = "active"
        if user.role == "user":
            user.role = "reseller"
    elif application.kind == "promoter":
        row = await session.get(Promoter, user.telegram_id)
        if row is None:
            session.add(
                Promoter(
                    user_id=user.telegram_id,
                    status="active",
                    created_at=utcnow(),
                    updated_at=utcnow(),
                )
            )
        else:
            row.status = "active"
        if user.role == "user":
            user.role = "promoter"
    await session.flush()


async def reject_application(session: AsyncSession, application: RoleApplication) -> None:
    application.status = "rejected"
    await session.flush()


async def request_payout(
    session: AsyncSession,
    user: User,
    *,
    kind: str,
    amount_paise: int,
) -> tuple[str, int]:
    from app.models import PayoutRequest
    from app.security import new_public_id

    if amount_paise < MIN_PAYOUT_PAISE:
        raise WalletError(f"Minimum payout is {paise_to_rupees_str(MIN_PAYOUT_PAISE)}")
    if int(user.available_paise) < amount_paise:
        raise WalletError("Insufficient available balance for this payout")
    public_id = new_public_id("PO", 6)
    await debit(
        session,
        user,
        amount_paise,
        reason=f"Payout request {public_id}",
        idempotency_key=new_idempotency_key("payout", public_id),
        reference_type="payout",
        reference_id=public_id,
        meta={"kind": kind},
    )
    session.add(
        PayoutRequest(
            public_id=public_id,
            user_id=user.telegram_id,
            kind=kind,
            amount_paise=amount_paise,
            status="pending",
            created_at=utcnow(),
        )
    )
    await session.flush()
    return public_id, amount_paise
