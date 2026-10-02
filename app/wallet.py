"""Immutable wallet ledger.

Every balance change is an append-only LedgerEntry. The User.cached_* columns
are updated in the same transaction and must never be written elsewhere.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import LedgerEntry, User
from app.security import new_idempotency_key, utcnow

log = logging.getLogger("falaron.wallet")

EntryType = Literal["credit", "debit", "reserve", "release", "capture"]


class WalletError(Exception):
    pass


class InsufficientFunds(WalletError):
    pass


class IdempotentReplay(WalletError):
    def __init__(self, entry: LedgerEntry) -> None:
        super().__init__("idempotent replay")
        self.entry = entry


@dataclass(frozen=True)
class WalletSnapshot:
    balance_paise: int
    reserved_paise: int
    available_paise: int

    def as_dict(self) -> dict[str, int]:
        return {
            "balance_paise": self.balance_paise,
            "reserved_paise": self.reserved_paise,
            "available_paise": self.available_paise,
        }


def snapshot_of(user: User) -> WalletSnapshot:
    return WalletSnapshot(
        balance_paise=int(user.cached_balance_paise),
        reserved_paise=int(user.cached_reserved_paise),
        available_paise=int(user.available_paise),
    )


async def get_or_create_user(
    session: AsyncSession,
    telegram_id: int,
    *,
    username: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    language_code: str | None = None,
    owner_id: int | None = None,
) -> User:
    from app.referrals import ensure_owner_partners, ensure_referral_code

    user = await session.get(User, telegram_id)
    if user is None:
        role = "owner" if owner_id is not None and telegram_id == owner_id else "user"
        user = User(
            telegram_id=telegram_id,
            username=username,
            first_name=first_name,
            last_name=last_name,
            language_code=(language_code or "en")[:8],
            role=role,
            created_at=utcnow(),
            updated_at=utcnow(),
        )
        try:
            async with session.begin_nested():
                session.add(user)
                await session.flush()
        except IntegrityError:
            # Two first requests raced; the other one won. Use its row.
            existing_row = await session.get(User, telegram_id)
            if existing_row is None:
                raise
            return existing_row
        await ensure_referral_code(session, user)
        if role == "owner":
            await ensure_owner_partners(session, user)
        return user
    changed = False
    if username is not None and user.username != username:
        user.username = username
        changed = True
    if first_name is not None and user.first_name != first_name:
        user.first_name = first_name
        changed = True
    if last_name is not None and user.last_name != last_name:
        user.last_name = last_name
        changed = True
    if language_code and user.language_code != language_code[:8]:
        user.language_code = language_code[:8]
        changed = True
    if owner_id is not None and telegram_id == owner_id and user.role != "owner":
        user.role = "owner"
        changed = True
    if not user.referral_code:
        await ensure_referral_code(session, user)
        changed = True
    if user.role in {"owner", "admin"} or (owner_id is not None and telegram_id == owner_id):
        await ensure_owner_partners(session, user)
    if changed:
        user.updated_at = utcnow()
    return user


async def lock_user(session: AsyncSession, user: User) -> User:
    """Serialise wallet mutations per user.

    PostgreSQL: `SELECT ... FOR UPDATE` takes a row lock until the transaction ends, so two
    concurrent orders/deposits cannot both read the same balance (double-spend). SQLite ignores
    FOR UPDATE, but it already serialises writers. Pending changes are flushed first so the
    refresh does not discard them, and the cached balances are re-read under the lock.
    """
    await session.flush()
    await session.refresh(user, with_for_update=True)
    return user


async def _existing(session: AsyncSession, idempotency_key: str) -> LedgerEntry | None:
    return (
        await session.execute(
            select(LedgerEntry).where(LedgerEntry.idempotency_key == idempotency_key)
        )
    ).scalar_one_or_none()


async def _append(
    session: AsyncSession,
    user: User,
    *,
    amount_paise: int,
    entry_type: EntryType,
    reason: str,
    idempotency_key: str,
    reference_type: str | None = None,
    reference_id: str | None = None,
    created_by: int | None = None,
    meta: dict[str, Any] | None = None,
    new_balance: int,
    new_reserved: int,
) -> LedgerEntry:
    if new_balance < 0 or new_reserved < 0:
        raise WalletError("Wallet invariant violated")
    if new_reserved > new_balance:
        raise InsufficientFunds("Reserved amount exceeds balance")
    entry = LedgerEntry(
        user_id=user.telegram_id,
        amount_paise=int(amount_paise),
        entry_type=entry_type,
        reason=reason[:255],
        reference_type=reference_type,
        reference_id=str(reference_id) if reference_id is not None else None,
        idempotency_key=idempotency_key,
        balance_after_paise=int(new_balance),
        reserved_after_paise=int(new_reserved),
        meta_json=json.dumps(meta, separators=(",", ":")) if meta else None,
        created_by=created_by,
        created_at=utcnow(),
    )
    user.cached_balance_paise = int(new_balance)
    user.cached_reserved_paise = int(new_reserved)
    user.updated_at = utcnow()
    session.add(entry)
    await session.flush()
    log.info(
        "ledger user=%s type=%s amount=%s bal=%s reserved=%s ref=%s/%s",
        user.telegram_id,
        entry_type,
        amount_paise,
        new_balance,
        new_reserved,
        reference_type,
        reference_id,
    )
    return entry


async def credit(
    session: AsyncSession,
    user: User,
    amount_paise: int,
    reason: str,
    *,
    idempotency_key: str | None = None,
    reference_type: str | None = None,
    reference_id: str | None = None,
    created_by: int | None = None,
    meta: dict[str, Any] | None = None,
) -> LedgerEntry:
    if amount_paise <= 0:
        raise WalletError("Credit amount must be positive")
    await lock_user(session, user)
    key = idempotency_key or new_idempotency_key("credit", str(user.telegram_id), reason, str(amount_paise))
    existing = await _existing(session, key)
    if existing is not None:
        return existing
    return await _append(
        session,
        user,
        amount_paise=amount_paise,
        entry_type="credit",
        reason=reason,
        idempotency_key=key,
        reference_type=reference_type,
        reference_id=reference_id,
        created_by=created_by,
        meta=meta,
        new_balance=int(user.cached_balance_paise) + amount_paise,
        new_reserved=int(user.cached_reserved_paise),
    )


async def debit(
    session: AsyncSession,
    user: User,
    amount_paise: int,
    reason: str,
    *,
    idempotency_key: str | None = None,
    reference_type: str | None = None,
    reference_id: str | None = None,
    created_by: int | None = None,
    meta: dict[str, Any] | None = None,
) -> LedgerEntry:
    if amount_paise <= 0:
        raise WalletError("Debit amount must be positive")
    await lock_user(session, user)
    key = idempotency_key or new_idempotency_key("debit", str(user.telegram_id), reason, str(amount_paise))
    existing = await _existing(session, key)
    if existing is not None:
        return existing
    available = int(user.available_paise)
    if available < amount_paise:
        raise InsufficientFunds(
            f"Need {amount_paise} paise, available {available}"
        )
    return await _append(
        session,
        user,
        amount_paise=-amount_paise,
        entry_type="debit",
        reason=reason,
        idempotency_key=key,
        reference_type=reference_type,
        reference_id=reference_id,
        created_by=created_by,
        meta=meta,
        new_balance=int(user.cached_balance_paise) - amount_paise,
        new_reserved=int(user.cached_reserved_paise),
    )


async def reserve(
    session: AsyncSession,
    user: User,
    amount_paise: int,
    reason: str,
    *,
    idempotency_key: str,
    reference_type: str | None = None,
    reference_id: str | None = None,
    meta: dict[str, Any] | None = None,
) -> LedgerEntry:
    if amount_paise <= 0:
        raise WalletError("Reserve amount must be positive")
    await lock_user(session, user)
    existing = await _existing(session, idempotency_key)
    if existing is not None:
        return existing
    if int(user.available_paise) < amount_paise:
        raise InsufficientFunds(
            f"Need {amount_paise} paise, available {int(user.available_paise)}"
        )
    return await _append(
        session,
        user,
        amount_paise=0,
        entry_type="reserve",
        reason=reason,
        idempotency_key=idempotency_key,
        reference_type=reference_type,
        reference_id=reference_id,
        meta=meta,
        new_balance=int(user.cached_balance_paise),
        new_reserved=int(user.cached_reserved_paise) + amount_paise,
    )


async def release(
    session: AsyncSession,
    user: User,
    amount_paise: int,
    reason: str,
    *,
    idempotency_key: str,
    reference_type: str | None = None,
    reference_id: str | None = None,
    meta: dict[str, Any] | None = None,
) -> LedgerEntry:
    if amount_paise <= 0:
        raise WalletError("Release amount must be positive")
    await lock_user(session, user)
    existing = await _existing(session, idempotency_key)
    if existing is not None:
        return existing
    reserved = int(user.cached_reserved_paise)
    if reserved < amount_paise:
        amount_paise = reserved
        if amount_paise <= 0:
            return await _append(
                session,
                user,
                amount_paise=0,
                entry_type="release",
                reason=reason,
                idempotency_key=idempotency_key,
                reference_type=reference_type,
                reference_id=reference_id,
                meta=meta,
                new_balance=int(user.cached_balance_paise),
                new_reserved=0,
            )
    return await _append(
        session,
        user,
        amount_paise=0,
        entry_type="release",
        reason=reason,
        idempotency_key=idempotency_key,
        reference_type=reference_type,
        reference_id=reference_id,
        meta=meta,
        new_balance=int(user.cached_balance_paise),
        new_reserved=reserved - amount_paise,
    )


async def capture(
    session: AsyncSession,
    user: User,
    amount_paise: int,
    reason: str,
    *,
    idempotency_key: str,
    reference_type: str | None = None,
    reference_id: str | None = None,
    meta: dict[str, Any] | None = None,
) -> LedgerEntry:
    """Convert a previous reserve into a real debit."""
    if amount_paise <= 0:
        raise WalletError("Capture amount must be positive")
    await lock_user(session, user)
    existing = await _existing(session, idempotency_key)
    if existing is not None:
        return existing
    reserved = int(user.cached_reserved_paise)
    if reserved < amount_paise:
        raise WalletError("Cannot capture more than the reserved amount")
    return await _append(
        session,
        user,
        amount_paise=-amount_paise,
        entry_type="capture",
        reason=reason,
        idempotency_key=idempotency_key,
        reference_type=reference_type,
        reference_id=reference_id,
        meta=meta,
        new_balance=int(user.cached_balance_paise) - amount_paise,
        new_reserved=reserved - amount_paise,
    )


async def list_entries(
    session: AsyncSession, user_id: int, *, limit: int = 20, offset: int = 0
) -> list[LedgerEntry]:
    result = await session.execute(
        select(LedgerEntry)
        .where(LedgerEntry.user_id == user_id)
        .order_by(LedgerEntry.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return list(result.scalars().all())
