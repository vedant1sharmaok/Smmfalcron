"""
/start handler — policy gate + main menu.
Section 4.1, 4.2, 37 of blueprint.
- First-time: show policy gate (must accept before accessing anything)
- Returning users: show main menu with balance
- Callbacks EDIT existing messages (Section 50.19)
"""
from __future__ import annotations
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.keyboards import (
    CB, back_home_keyboard, main_menu_keyboard,
    policy_keyboard, policy_view_keyboard, policies_keyboard,
)
from app.bot.messages import (
    PRIVACY_TEXT, REFUND_TEXT, TERMS_TEXT,
    policy_gate, welcome_message,
)
from app.bot.safe_send import safe_edit, safe_send
from app.core.logging import get_logger

logger = get_logger(__name__)
router = Router(name="start")

_CURRENT_POLICY_VERSION = 1


async def _get_or_create_user(db, tg_user):
    """Get or create platform user from Telegram user object."""
    from sqlalchemy import select
    from app.core.models import User, Wallet
    from app.core.database import AsyncSessionLocal

    result = await db.execute(select(User).where(User.telegram_id == tg_user.id))
    user   = result.scalar_one_or_none()

    if user is None:
        now  = datetime.now(timezone.utc)
        user = User(
            telegram_id=tg_user.id,
            first_name=tg_user.first_name or "",
            last_name=tg_user.last_name or "",
            username=tg_user.username or "",
            language_code=tg_user.language_code or "en",
            is_active=True,
            is_banned=False,
            is_premium=False,
            policy_version=0,
            created_at=now,
            updated_at=now,
        )
        db.add(user)
        await db.flush()

        wallet = Wallet(
            user_id=user.id,
            balance=Decimal("0"),
            currency="INR",
            created_at=now,
            updated_at=now,
        )
        db.add(wallet)
        await db.commit()
        logger.info("user_created", telegram_id=tg_user.id)

    # Update last activity
    user.last_activity_at = datetime.now(timezone.utc)
    await db.commit()
    return user


async def _get_wallet_balance(db, user_id: int) -> Decimal:
    from sqlalchemy import select
    from app.core.models import Wallet
    result = await db.execute(select(Wallet).where(Wallet.user_id == user_id))
    wallet = result.scalar_one_or_none()
    return wallet.balance if wallet else Decimal("0")


async def _get_order_count(db, user_id: int) -> int:
    from sqlalchemy import select, func
    from app.core.models import Order
    result = await db.execute(
        select(func.count()).where(Order.user_id == user_id)
    )
    return result.scalar_one() or 0


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    tg_user = message.from_user
    if not tg_user:
        return

    from app.core.database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        user    = await _get_or_create_user(db, tg_user)
        if user.is_banned:
            await message.answer("⛔ Your account has been suspended. Contact support.")
            return

        # Policy gate — must accept before proceeding
        if (user.policy_version or 0) < _CURRENT_POLICY_VERSION:
            await message.answer(
                policy_gate(tg_user.first_name or "there"),
                reply_markup=policy_keyboard(),
                parse_mode="HTML",
            )
            return

        balance     = await _get_wallet_balance(db, user.id)
        order_count = await _get_order_count(db, user.id)

    await message.answer(
        welcome_message(
            first_name=tg_user.first_name or "there",
            balance=balance,
            is_premium=user.is_premium,
            order_count=order_count,
        ),
        reply_markup=main_menu_keyboard(),
        parse_mode="HTML",
    )


# ── Policy callbacks ───────────────────────────────────────────────────────────

@router.callback_query(F.data == CB.POLICY_TOS)
async def policy_tos(call: CallbackQuery) -> None:
    await call.answer()
    await safe_edit(call.message, TERMS_TEXT, reply_markup=policy_keyboard())


@router.callback_query(F.data == CB.POLICY_PRIVACY)
async def policy_privacy(call: CallbackQuery) -> None:
    await call.answer()
    await safe_edit(call.message, PRIVACY_TEXT, reply_markup=policy_keyboard())


@router.callback_query(F.data == CB.POLICY_REFUND)
async def policy_refund(call: CallbackQuery) -> None:
    await call.answer()
    await safe_edit(call.message, REFUND_TEXT, reply_markup=policy_keyboard())


@router.callback_query(F.data == CB.POLICY_ACCEPT)
async def policy_accept(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer("✅ Policies accepted!")
    tg_user = call.from_user

    from app.core.database import AsyncSessionLocal
    from sqlalchemy import select, update
    from app.core.models import User

    async with AsyncSessionLocal() as db:
        await db.execute(
            update(User)
            .where(User.telegram_id == tg_user.id)
            .values(policy_version=_CURRENT_POLICY_VERSION)
        )
        await db.commit()
        result  = (await db.execute(select(User).where(User.telegram_id == tg_user.id))).scalar_one_or_none()
        balance = await _get_wallet_balance(db, result.id) if result else Decimal("0")
        orders  = await _get_order_count(db, result.id) if result else 0

    await safe_edit(
        call.message,
        welcome_message(
            first_name=tg_user.first_name or "there",
            balance=balance,
            is_premium=result.is_premium if result else False,
            order_count=orders,
        ),
        reply_markup=main_menu_keyboard(),
    )


# ── Home callback ─────────────────────────────────────────────────────────────

@router.callback_query(F.data == CB.HOME)
async def go_home(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.clear()
    tg_user = call.from_user

    from app.core.database import AsyncSessionLocal
    from sqlalchemy import select
    from app.core.models import User

    async with AsyncSessionLocal() as db:
        result  = (await db.execute(select(User).where(User.telegram_id == tg_user.id))).scalar_one_or_none()
        balance = await _get_wallet_balance(db, result.id) if result else Decimal("0")
        orders  = await _get_order_count(db, result.id) if result else 0

    await safe_edit(
        call.message,
        welcome_message(
            first_name=tg_user.first_name or "there",
            balance=balance if result else Decimal("0"),
            is_premium=result.is_premium if result else False,
            order_count=orders,
        ),
        reply_markup=main_menu_keyboard(),
    )


# ── Policies menu ─────────────────────────────────────────────────────────────

@router.callback_query(F.data == CB.POLICIES)
async def show_policies(call: CallbackQuery) -> None:
    await call.answer()
    await safe_edit(
        call.message,
        "📜 <b>Policies</b>\n\nChoose a policy to view:",
        reply_markup=policies_keyboard(),
    )

@router.callback_query(F.data == CB.POL_TOS)
async def show_tos(call: CallbackQuery) -> None:
    await call.answer()
    await safe_edit(call.message, TERMS_TEXT, reply_markup=policies_keyboard())

@router.callback_query(F.data == CB.POL_PRIVACY)
async def show_privacy(call: CallbackQuery) -> None:
    await call.answer()
    await safe_edit(call.message, PRIVACY_TEXT, reply_markup=policies_keyboard())

@router.callback_query(F.data == CB.POL_REFUND)
async def show_refund_policy(call: CallbackQuery) -> None:
    await call.answer()
    await safe_edit(call.message, REFUND_TEXT, reply_markup=policies_keyboard())
