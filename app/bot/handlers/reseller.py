"""Reseller panel: dashboard, discounted orders, downline, payout, apply."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.handlers.start import ensure_user
from app.bot.menus import (
    RsCB,
    apply_kb,
    back_home_kb,
    html_escape,
    reseller_home_kb,
    safe_edit,
)
from app.config import Settings
from app.models import MIN_PAYOUT_PAISE, User
from app.pricing import paise_to_rupees_str
from app.referrals import (
    is_reseller_user,
    pending_application,
    request_payout,
    reseller_snapshot,
    submit_application,
)
from app.wallet import WalletError

router = Router(name="reseller")


class ResellerApply(StatesGroup):
    pitch = State()


class ResellerPayout(StatesGroup):
    amount = State()


def _dashboard_text(snap) -> str:
    pct = snap.discount_bp / 100.0
    return (
        "<b>Reseller panel</b>\n\n"
        f"Tier: {html_escape(snap.tier.title())}\n"
        f"Status: {html_escape(snap.status)}\n"
        f"Your discount: <b>{pct:.1f}%</b> (never below 10% margin vs cost)\n"
        f"Orders: {snap.orders}\n"
        f"GMV: {paise_to_rupees_str(snap.gmv_paise)}\n"
        f"Wallet: {paise_to_rupees_str(snap.wallet_paise)}\n"
        f"Downline: {snap.downline}\n\n"
        "Place orders from Services — your reseller price is applied automatically."
    )


@router.callback_query(RsCB.filter(F.a == "home"))
async def cb_reseller_home(
    query: CallbackQuery, session: AsyncSession, settings: Settings, state: FSMContext
) -> None:
    await state.clear()
    if query.from_user is None:
        return
    user = await ensure_user(session, query.from_user, settings)
    if not await is_reseller_user(session, user, settings.owner_telegram_id):
        pending = await pending_application(session, user.telegram_id, "reseller")
        if pending is not None:
            await safe_edit(
                query,
                "<b>Reseller</b>\nYour application is pending admin review.",
                back_home_kb(),
            )
            return
        await safe_edit(
            query,
            "<b>Reseller</b>\nApply to buy at a panel discount (default 12%). "
            "Admin approves applications. The owner is auto-approved.",
            apply_kb("reseller"),
        )
        return
    snap = await reseller_snapshot(session, user)
    await safe_edit(query, _dashboard_text(snap), reseller_home_kb(can_order=True))


@router.callback_query(RsCB.filter(F.a == "apply"))
async def cb_reseller_apply(
    query: CallbackQuery, session: AsyncSession, settings: Settings, state: FSMContext
) -> None:
    if query.from_user is None:
        return
    user = await ensure_user(session, query.from_user, settings)
    if await is_reseller_user(session, user, settings.owner_telegram_id):
        snap = await reseller_snapshot(session, user)
        await safe_edit(query, _dashboard_text(snap), reseller_home_kb(can_order=True))
        return
    pending = await pending_application(session, user.telegram_id, "reseller")
    if pending is not None:
        await safe_edit(query, "<b>Reseller</b>\nAlready pending review.", back_home_kb())
        return
    await state.set_state(ResellerApply.pitch)
    await safe_edit(
        query,
        "<b>Reseller application</b>\nSend a short pitch (who you resell to, volume). /cancel to abort.",
        back_home_kb(),
    )


@router.message(ResellerApply.pitch)
async def reseller_got_pitch(
    message: Message, session: AsyncSession, settings: Settings, state: FSMContext
) -> None:
    if message.from_user is None or not message.text:
        return
    if message.text.startswith("/cancel"):
        await state.clear()
        await message.answer("Cancelled.")
        return
    pitch = message.text.strip()
    if len(pitch) < 8:
        await message.answer("Please write at least a sentence.")
        return
    user = await ensure_user(session, message.from_user, settings)
    await submit_application(session, user, "reseller", pitch)
    await state.clear()
    await message.answer(
        "Application received. An admin will review it. "
        "(Demo: the owner is already an active reseller.)",
        parse_mode="HTML",
        reply_markup=back_home_kb(),
    )


@router.callback_query(RsCB.filter(F.a == "down"))
async def cb_reseller_downline(
    query: CallbackQuery, session: AsyncSession, settings: Settings
) -> None:
    if query.from_user is None:
        return
    user = await ensure_user(session, query.from_user, settings)
    if not await is_reseller_user(session, user, settings.owner_telegram_id):
        await query.answer("Not a reseller", show_alert=True)
        return
    rows = list(
        (
            await session.execute(
                select(User).where(User.referred_by == user.telegram_id).order_by(User.created_at.desc()).limit(20)
            )
        ).scalars().all()
    )
    if not rows:
        await safe_edit(query, "<b>Downline</b>\nNo referred accounts yet.", reseller_home_kb(can_order=True))
        return
    lines = ["<b>Downline</b>", ""]
    for row in rows:
        uname = f"@{row.username}" if row.username else "—"
        lines.append(
            f"• {html_escape(row.display_name)} · {html_escape(uname)} · "
            f"<code>{row.telegram_id}</code>"
        )
    await safe_edit(query, "\n".join(lines), reseller_home_kb(can_order=True))


@router.callback_query(RsCB.filter(F.a == "pay"))
async def cb_reseller_payout(
    query: CallbackQuery, session: AsyncSession, settings: Settings, state: FSMContext
) -> None:
    if query.from_user is None:
        return
    user = await ensure_user(session, query.from_user, settings)
    if not await is_reseller_user(session, user, settings.owner_telegram_id):
        await query.answer("Not a reseller", show_alert=True)
        return
    await state.set_state(ResellerPayout.amount)
    await safe_edit(
        query,
        f"<b>Payout</b>\nAvailable {paise_to_rupees_str(int(user.available_paise))}.\n"
        f"Minimum {paise_to_rupees_str(MIN_PAYOUT_PAISE)}. Send amount in INR, or /cancel.",
        back_home_kb(),
    )


@router.message(ResellerPayout.amount)
async def reseller_got_amount(
    message: Message, session: AsyncSession, settings: Settings, state: FSMContext
) -> None:
    if message.from_user is None or not message.text:
        return
    if message.text.startswith("/cancel"):
        await state.clear()
        await message.answer("Cancelled.")
        return
    raw = message.text.replace(",", "").replace("₹", "").strip()
    try:
        rupees = float(raw)
    except ValueError:
        await message.answer("Send a number, e.g. 500.")
        return
    paise = int(round(rupees * 100))
    user = await ensure_user(session, message.from_user, settings)
    try:
        public_id, amount = await request_payout(session, user, kind="reseller", amount_paise=paise)
    except WalletError as exc:
        await message.answer(str(exc))
        return
    await state.clear()
    await message.answer(
        f"Payout request <code>{html_escape(public_id)}</code> for "
        f"{paise_to_rupees_str(amount)} submitted. Funds reserved from your wallet.",
        parse_mode="HTML",
        reply_markup=back_home_kb(),
    )
