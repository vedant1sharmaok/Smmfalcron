"""
Deposit, wallet, balance, profile, stats, premium, rewards, support, settings handlers.
Sections 17, 18-19, 36 of blueprint.
"""
from __future__ import annotations
from decimal import Decimal
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from app.bot.keyboards import CB, back_home_keyboard, wallet_keyboard, deposit_keyboard, premium_keyboard
from app.bot.messages import (
    wallet_card, deposit_prompt, deposit_custom_prompt, deposit_pending,
    insufficient_balance, balance_ledger, profile_card, stats_card,
    premium_status, rewards_card, support_card, settings_card, error_card,
)
from app.bot.safe_send import safe_edit
from app.core.logging import get_logger

logger = get_logger(__name__)
router = Router(name="deposit")


class DepositFSM(StatesGroup):
    entering_amount = State()


async def _get_user(db, telegram_id: int):
    from sqlalchemy import select
    from app.core.models import User
    return (await db.execute(select(User).where(User.telegram_id == telegram_id))).scalar_one_or_none()


async def _get_wallet(db, user_id: int):
    from sqlalchemy import select
    from app.core.models import Wallet
    return (await db.execute(select(Wallet).where(Wallet.user_id == user_id))).scalar_one_or_none()


# ── Wallet ────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == CB.WALLET)
async def show_wallet(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.clear()
    from app.core.database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        user   = await _get_user(db, call.from_user.id)
        wallet = await _get_wallet(db, user.id) if user else None
    balance = wallet.balance if wallet else Decimal("0")
    await safe_edit(call.message, wallet_card(balance), reply_markup=wallet_keyboard())


# ── Deposit ───────────────────────────────────────────────────────────────────

@router.callback_query(F.data == CB.DEPOSIT)
async def show_deposit_menu(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.clear()
    await safe_edit(call.message, deposit_prompt(), reply_markup=deposit_keyboard())


@router.callback_query(F.data.startswith("dep:amt:"))
async def handle_deposit_preset(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    amount = Decimal(call.data.split(":")[2])
    await _initiate_deposit(call, state, amount)


@router.callback_query(F.data == CB.DEP_CUSTOM)
async def deposit_custom(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await safe_edit(call.message, deposit_custom_prompt(), reply_markup=back_home_keyboard())
    await state.set_state(DepositFSM.entering_amount)


@router.message(DepositFSM.entering_amount)
async def receive_custom_amount(message: Message, state: FSMContext) -> None:
    try:
        amount = Decimal(message.text.strip().replace(",", "").replace("₹", ""))
        if amount < 10:
            await message.answer(error_card("Minimum deposit is ₹10."), parse_mode="HTML"); return
        if amount > 100000:
            await message.answer(error_card("Maximum deposit is ₹1,00,000."), parse_mode="HTML"); return
    except Exception:
        await message.answer(error_card("Please enter a valid amount (e.g. 500)."), parse_mode="HTML"); return
    await state.clear()
    # Create payment and send link
    from app.core.database import AsyncSessionLocal
    import uuid, datetime
    async with AsyncSessionLocal() as db:
        user = await _get_user(db, message.from_user.id)
        if not user:
            await message.answer(error_card("Session expired. Send /start."), parse_mode="HTML"); return
        from app.core.models import Payment
        from datetime import datetime as dt, timezone
        payment = Payment(
            user_id=user.id, amount=amount, currency="INR",
            provider="razorpay", provider_ref=f"SMM-{uuid.uuid4().hex[:8].upper()}",
            status="pending", webhook_verified=False,
            created_at=dt.now(timezone.utc),
        )
        db.add(payment); await db.commit()

    await message.answer(
        deposit_pending(amount, "Razorpay"),
        reply_markup=back_home_keyboard(), parse_mode="HTML",
    )


async def _initiate_deposit(call: CallbackQuery, state: FSMContext, amount: Decimal) -> None:
    from app.core.database import AsyncSessionLocal
    import uuid
    from datetime import datetime, timezone
    async with AsyncSessionLocal() as db:
        user = await _get_user(db, call.from_user.id)
        if not user:
            await safe_edit(call.message, error_card("Session expired."), reply_markup=back_home_keyboard()); return
        from app.core.models import Payment
        payment = Payment(
            user_id=user.id, amount=amount, currency="INR",
            provider="razorpay", provider_ref=f"SMM-{uuid.uuid4().hex[:8].upper()}",
            status="pending", webhook_verified=False,
            created_at=datetime.now(timezone.utc),
        )
        db.add(payment); await db.commit()

    await safe_edit(
        call.message,
        deposit_pending(amount, "Razorpay"),
        reply_markup=back_home_keyboard(),
    )


# ── Balance summary ───────────────────────────────────────────────────────────

@router.callback_query(F.data == CB.BALANCE)
async def show_balance(call: CallbackQuery) -> None:
    await call.answer()
    from app.core.database import AsyncSessionLocal
    from sqlalchemy import select, func
    from app.core.models import WalletTransaction, Wallet
    async with AsyncSessionLocal() as db:
        user   = await _get_user(db, call.from_user.id)
        wallet = await _get_wallet(db, user.id) if user else None
        balance = wallet.balance if wallet else Decimal("0")

        deposited = refunded = spent = Decimal("0")
        if wallet:
            txs = list((await db.execute(
                select(WalletTransaction).where(WalletTransaction.wallet_id == wallet.id)
            )).scalars().all())
            for tx in txs:
                if tx.tx_type in ("DEPOSIT", "ADMIN_CREDIT", "BONUS_CREDIT", "COUPON_CREDIT"):
                    deposited += tx.amount
                elif tx.tx_type in ("REFUND",):
                    refunded  += tx.amount
                elif tx.tx_type in ("ORDER_DEBIT", "API_DEBIT"):
                    spent     += tx.amount

    await safe_edit(call.message,
        balance_ledger(balance, deposited, spent, refunded),
        reply_markup=back_home_keyboard())


# ── Profile ───────────────────────────────────────────────────────────────────

@router.callback_query(F.data == CB.PROFILE)
async def show_profile(call: CallbackQuery) -> None:
    await call.answer()
    from app.core.database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        user   = await _get_user(db, call.from_user.id)
        wallet = await _get_wallet(db, user.id) if user else None
        balance= wallet.balance if wallet else Decimal("0")
        plan   = "basic"
        if user and user.is_premium:
            from app.premium.service import get_plan
            plan = await get_plan(db, user.id)

    if not user:
        await safe_edit(call.message, error_card("Session expired."), reply_markup=back_home_keyboard()); return

    since = user.created_at.strftime("%b %Y") if user.created_at else "—"
    await safe_edit(call.message,
        profile_card(
            first_name=call.from_user.first_name or "User",
            username=call.from_user.username,
            telegram_id=call.from_user.id,
            balance=balance,
            is_premium=user.is_premium,
            plan=plan,
            member_since=since,
        ),
        reply_markup=back_home_keyboard())


# ── Stats ─────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == CB.STATS)
async def show_stats(call: CallbackQuery) -> None:
    await call.answer()
    from app.core.database import AsyncSessionLocal
    from sqlalchemy import select, func
    from app.core.models import Order, WalletTransaction, Wallet
    async with AsyncSessionLocal() as db:
        user   = await _get_user(db, call.from_user.id)
        if not user:
            await safe_edit(call.message, error_card("Session expired."), reply_markup=back_home_keyboard()); return

        total = (await db.execute(select(func.count()).where(Order.user_id == user.id))).scalar_one() or 0
        done  = (await db.execute(select(func.count()).where(Order.user_id == user.id, Order.status == "completed"))).scalar_one() or 0
        proc  = (await db.execute(select(func.count()).where(Order.user_id == user.id, Order.status.in_(["processing","pending","in_progress"])))).scalar_one() or 0
        failed= (await db.execute(select(func.count()).where(Order.user_id == user.id, Order.status == "failed"))).scalar_one() or 0
        refills=(await db.execute(select(func.count()).where(Order.user_id == user.id, Order.status == "refill"))).scalar_one() or 0

        wallet = await _get_wallet(db, user.id)
        deposited = spent = refunded = Decimal("0")
        if wallet:
            txs = list((await db.execute(select(WalletTransaction).where(WalletTransaction.wallet_id == wallet.id))).scalars().all())
            for tx in txs:
                if tx.tx_type in ("DEPOSIT","ADMIN_CREDIT","BONUS_CREDIT"): deposited += tx.amount
                elif tx.tx_type == "REFUND":                                 refunded  += tx.amount
                elif tx.tx_type in ("ORDER_DEBIT","API_DEBIT"):              spent     += tx.amount

    await safe_edit(call.message,
        stats_card(total, done, proc, failed, spent, deposited, refunded, refills),
        reply_markup=back_home_keyboard())


# ── Premium ───────────────────────────────────────────────────────────────────

@router.callback_query(F.data == CB.PREMIUM)
async def show_premium(call: CallbackQuery) -> None:
    await call.answer()
    from app.core.database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        user = await _get_user(db, call.from_user.id)
        if not user:
            await safe_edit(call.message, error_card("Session expired."), reply_markup=back_home_keyboard()); return
        plan = "basic"
        expires = None
        if user.is_premium:
            from app.premium.service import get_plan, get_active_premium
            plan   = await get_plan(db, user.id)
            record = await get_active_premium(db, user.id)
            if record and record.expires_at:
                expires = record.expires_at.strftime("%Y-%m-%d")

    await safe_edit(call.message,
        premium_status(user.is_premium, plan, expires),
        reply_markup=premium_keyboard(user.is_premium))


@router.callback_query(F.data.startswith("prem:buy:"))
async def buy_premium(call: CallbackQuery) -> None:
    plan = call.data.split(":")[2]
    prices = {"premium": "₹499/month", "vip": "₹999/month"}
    await call.answer(f"Contact support to subscribe to {plan.title()} ({prices.get(plan,'')}).", show_alert=True)


# ── Rewards ───────────────────────────────────────────────────────────────────

@router.callback_query(F.data == CB.REWARDS)
async def show_rewards(call: CallbackQuery) -> None:
    await call.answer()
    tg_id = call.from_user.id
    code  = f"SMM{tg_id}"[-10:]
    await safe_edit(call.message,
        rewards_card(referral_code=code, referrals=0, reward_balance=Decimal("0")),
        reply_markup=back_home_keyboard())


# ── Support ───────────────────────────────────────────────────────────────────

@router.callback_query(F.data == CB.SUPPORT)
async def show_support(call: CallbackQuery) -> None:
    await call.answer()
    from app.core.config import settings
    support_user = getattr(settings, "support_username", "")
    await safe_edit(call.message, support_card(support_user), reply_markup=back_home_keyboard())


# ── Settings ──────────────────────────────────────────────────────────────────

@router.callback_query(F.data == CB.SETTINGS)
async def show_settings(call: CallbackQuery) -> None:
    await call.answer()
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    kb = InlineKeyboardBuilder()
    kb.button(text="🌐 Language",     callback_data="settings:lang")
    kb.button(text="🔔 Notifications", callback_data="settings:notif")
    kb.button(text="🏠 Main Menu",    callback_data=CB.HOME)
    kb.adjust(1)
    await safe_edit(call.message, settings_card(), reply_markup=kb.as_markup())
