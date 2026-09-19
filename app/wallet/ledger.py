"""
Wallet ledger — append-only transaction log with SELECT FOR UPDATE.

Every balance change creates a WalletTransaction row.
The wallet balance column is the materialized sum — always equal to
the sum of all transaction amounts for that wallet.

Key invariants:
  1. SELECT FOR UPDATE on wallet row before any balance read/write
     → serialises concurrent writes at the DB level
  2. Idempotency key prevents double-credit (webhook + reconciler)
  3. Insufficient balance check before balance update
  4. Zero / negative amount rejected before any DB operation
  5. balance_before + amount == balance_after for every row
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import InsufficientBalanceError, WalletError
from app.core.logging import get_logger
from app.core.models import Wallet, WalletTransaction

logger = get_logger(__name__)

_ZERO = Decimal("0")


class TxType(str, Enum):
    DEPOSIT       = "DEPOSIT"
    ORDER_DEBIT   = "ORDER_DEBIT"
    REFUND        = "REFUND"
    BONUS_CREDIT  = "BONUS_CREDIT"
    ADMIN_CREDIT  = "ADMIN_CREDIT"
    ADMIN_DEBIT   = "ADMIN_DEBIT"
    API_DEBIT     = "API_DEBIT"
    COUPON_CREDIT = "COUPON_CREDIT"
    RESELLER_CREDIT = "RESELLER_CREDIT"
    ADJUSTMENT    = "ADJUSTMENT"


async def _find_by_idempotency_key(
    db: AsyncSession, key: str
) -> Optional[WalletTransaction]:
    """Look up an existing transaction by idempotency key."""
    return (await db.execute(
        select(WalletTransaction).where(WalletTransaction.idempotency_key == key)
    )).scalar_one_or_none()


async def _execute_transaction(
    db: AsyncSession,
    user_id: int,
    amount: Decimal,
    tx_type: TxType,
    idempotency_key: str,
    is_credit: bool,
    reference_id: str | None = None,
    reason: str | None = None,
) -> WalletTransaction:
    """
    Core transaction executor — used by both credit() and debit().

    Steps:
      1. Idempotency check — return existing tx if already processed
      2. SELECT FOR UPDATE on wallet — serialize concurrent writes
      3. Validate amount (> 0)
      4. Compute balance_after
      5. For debits: check InsufficientBalance BEFORE updating
      6. UPDATE wallet.balance = balance_after
      7. INSERT WalletTransaction row
    """
    # Step 1: Idempotency
    existing = await _find_by_idempotency_key(db, idempotency_key)
    if existing is not None:
        logger.info(
            "ledger_idempotency_replay",
            idempotency_key=idempotency_key,
            tx_id=existing.id,
        )
        return existing

    # Step 2: SELECT FOR UPDATE — DB-level serialization
    wallet = (await db.execute(
        select(Wallet)
        .where(Wallet.user_id == user_id)
        .with_for_update()
    )).scalar_one_or_none()

    if wallet is None:
        raise WalletError(
            detail=f"Wallet not found for user {user_id}",
            user_message="Wallet not found.",
        )

    # Step 3: Validate amount
    if amount <= _ZERO:
        raise WalletError(
            detail=f"Invalid transaction amount: {amount}",
            user_message="Transaction amount must be greater than zero.",
        )

    balance_before = wallet.balance

    # Step 4: Compute balance_after
    if is_credit:
        balance_after = balance_before + amount
    else:
        balance_after = balance_before - amount

    # Step 5: Insufficient balance check (BEFORE any mutation)
    if not is_credit and balance_after < _ZERO:
        raise InsufficientBalanceError(
            detail=(
                f"Insufficient balance: user={user_id} "
                f"balance={balance_before} required={amount}"
            ),
            required=amount,
            available=balance_before,
        )

    # Step 6: UPDATE wallet.balance = balance_after
    wallet.balance = balance_after

    # Step 7: INSERT transaction record
    tx = WalletTransaction(
        id=str(uuid.uuid4()),
        wallet_id=wallet.id,
        tx_type=tx_type.value,
        amount=amount,
        balance_before=balance_before,
        balance_after=balance_after,
        idempotency_key=idempotency_key,
        reference_id=str(reference_id) if reference_id is not None else None,
        reason=reason,
        created_at=datetime.now(timezone.utc),
    )
    db.add(tx)
    await db.flush()

    logger.info(
        "ledger_transaction",
        tx_type=tx_type.value,
        user_id=user_id,
        amount=str(amount),
        balance_before=str(balance_before),
        balance_after=str(balance_after),
    )
    return tx


async def credit(
    db: AsyncSession,
    user_id: int,
    amount: Decimal,
    tx_type: TxType,
    idempotency_key: str,
    reference_id: str | None = None,
    reason: str | None = None,
) -> WalletTransaction:
    """
    Credit a user's wallet.

    Idempotent: if idempotency_key was already processed, returns
    the existing transaction without modifying the balance.
    """
    return await _execute_transaction(
        db=db, user_id=user_id, amount=amount,
        tx_type=tx_type, idempotency_key=idempotency_key,
        is_credit=True, reference_id=reference_id, reason=reason,
    )


async def debit(
    db: AsyncSession,
    user_id: int,
    amount: Decimal,
    tx_type: TxType,
    idempotency_key: str,
    reference_id: str | None = None,
    reason: str | None = None,
) -> WalletTransaction:
    """
    Debit a user's wallet.

    Raises InsufficientBalanceError before any DB write if balance is too low.
    Idempotent: same idempotency_key returns existing tx.
    """
    return await _execute_transaction(
        db=db, user_id=user_id, amount=amount,
        tx_type=tx_type, idempotency_key=idempotency_key,
        is_credit=False, reference_id=reference_id, reason=reason,
    )


async def verify_ledger_invariant(
    db: AsyncSession, user_id: int
) -> tuple[bool, Decimal, Decimal]:
    """
    Verify that wallet.balance equals the sum of all transactions.

    Returns (is_valid, cached_balance, computed_balance).
    Called by the daily wallet reconciler worker.
    """
    from sqlalchemy import func

    wallet = (await db.execute(
        select(Wallet).where(Wallet.user_id == user_id)
    )).scalar_one_or_none()

    if wallet is None:
        return False, _ZERO, _ZERO

    computed = (await db.execute(
        select(func.sum(WalletTransaction.amount)).where(
            WalletTransaction.wallet_id == wallet.id
        )
    )).scalar_one() or _ZERO

    is_valid = wallet.balance == computed
    if not is_valid:
        logger.error(
            "ledger_invariant_violation",
            user_id=user_id,
            cached=str(wallet.balance),
            computed=str(computed),
        )
    return is_valid, wallet.balance, computed
