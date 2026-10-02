"""Promoter panel: clicks, signups, share kit, payout, apply."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.handlers.start import ensure_user
from app.bot.menus import PrCB, apply_kb, back_home_kb, html_escape, promoter_home_kb, safe_edit
from app.bot.share import deep_link, resolve_bot_username
from app.config import Settings
from app.models import MIN_PAYOUT_PAISE
from app.pricing import paise_to_rupees_str
from app.referrals import (
    is_promoter_user,
    pending_application,
    promoter_snapshot,
    request_payout,
    submit_application,
)
from app.wallet import WalletError

router = Router(name="promoter")


class PromoterApply(StatesGroup):
    pitch = State()


class PromoterPayout(StatesGroup):
    amount = State()


def _dashboard_text(snap, link: str) -> str:
    return (
        "<b>Promoter panel</b>\n\n"
        f"Code: <code>{html_escape(snap.code)}</code>\n"
        f"Deep link: <code>{html_escape(link)}</code>\n\n"
        f"Clicks: {snap.clicks}\n"
        f"Signups: {snap.signups}\n"
        f"Converting users: {snap.converting}\n"
        f"Commission earned: {paise_to_rupees_str(snap.commission_paise)}\n"
        f"Available payout: {paise_to_rupees_str(snap.available_paise)}\n\n"
        "Commissions: 8% of a referred user's first 3 completed orders "
        "and 5% of their first deposit."
    )


@router.callback_query(PrCB.filter(F.a == "home"))
async def cb_promoter_home(
    query: CallbackQuery, session: AsyncSession, settings: Settings, state: FSMContext
) -> None:
    await state.clear()
    if query.from_user is None:
        return
    user = await ensure_user(session, query.from_user, settings)
    if not await is_promoter_user(session, user, settings.owner_telegram_id):
        pending = await pending_application(session, user.telegram_id, "promoter")
        if pending is not None:
            await safe_edit(
                query,
                "<b>Promoter</b>\nYour application is pending admin review.",
                back_home_kb(),
            )
            return
        await safe_edit(
            query,
            "<b>Promoter</b>\nApply to run a tracked FALARON invite. "
            "Admin approves. The owner is auto-approved.",
            apply_kb("promoter"),
        )
        return
    snap = await promoter_snapshot(session, user)
    username = await resolve_bot_username(query, settings.bot_username)
    link = deep_link(username, snap.code)
    await safe_edit(query, _dashboard_text(snap, link), promoter_home_kb())


@router.callback_query(PrCB.filter(F.a == "apply"))
async def cb_promoter_apply(
    query: CallbackQuery, session: AsyncSession, settings: Settings, state: FSMContext
) -> None:
    if query.from_user is None:
        return
    user = await ensure_user(session, query.from_user, settings)
    if await is_promoter_user(session, user, settings.owner_telegram_id):
        snap = await promoter_snapshot(session, user)
        username = await resolve_bot_username(query, settings.bot_username)
        await safe_edit(
            query,
            _dashboard_text(snap, deep_link(username, snap.code)),
            promoter_home_kb(),
        )
        return
    pending = await pending_application(session, user.telegram_id, "promoter")
    if pending is not None:
        await safe_edit(query, "<b>Promoter</b>\nAlready pending review.", back_home_kb())
        return
    await state.set_state(PromoterApply.pitch)
    await safe_edit(
        query,
        "<b>Promoter application</b>\nSend a short pitch (channels, audience). /cancel to abort.",
        back_home_kb(),
    )


@router.message(PromoterApply.pitch)
async def promoter_got_pitch(
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
    await submit_application(session, user, "promoter", pitch)
    await state.clear()
    await message.answer(
        "Application received. An admin will review it. "
        "(Demo: the owner is already an active promoter.)",
        parse_mode="HTML",
        reply_markup=back_home_kb(),
    )


@router.callback_query(PrCB.filter(F.a == "pay"))
async def cb_promoter_payout(
    query: CallbackQuery, session: AsyncSession, settings: Settings, state: FSMContext
) -> None:
    if query.from_user is None:
        return
    user = await ensure_user(session, query.from_user, settings)
    if not await is_promoter_user(session, user, settings.owner_telegram_id):
        await query.answer("Not a promoter", show_alert=True)
        return
    await state.set_state(PromoterPayout.amount)
    await safe_edit(
        query,
        f"<b>Payout</b>\nAvailable {paise_to_rupees_str(int(user.available_paise))}.\n"
        f"Minimum {paise_to_rupees_str(MIN_PAYOUT_PAISE)}. Send amount in INR, or /cancel.",
        back_home_kb(),
    )


@router.message(PromoterPayout.amount)
async def promoter_got_amount(
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
        public_id, amount = await request_payout(session, user, kind="promoter", amount_paise=paise)
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
