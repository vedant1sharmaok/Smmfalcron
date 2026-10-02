"""Referral rewards, share popup, and caption cards."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.ads import apply_ad
from app.bot.handlers.start import ensure_user
from app.bot.menus import MenuCB, ShareCB, html_escape, rewards_kb, safe_edit, share_picker_kb
from app.bot.share import captions_for, deep_link, picker_text, resolve_bot_username, send_share_card
from app.config import Settings
from app.pricing import paise_to_rupees_str
from app.referrals import referral_snapshot

router = Router(name="rewards")


async def _rewards_body(session: AsyncSession, user, settings: Settings, bot_username: str) -> str:
    snap = await referral_snapshot(session, user)
    link = deep_link(bot_username, snap.code)
    return (
        "<b>Rewards</b>\n\n"
        f"Your code: <code>{html_escape(snap.code)}</code>\n"
        f"Deep link: <code>{html_escape(link)}</code>\n\n"
        f"Invited: {snap.invited}\n"
        f"Earnings: {paise_to_rupees_str(snap.earnings_paise)}\n"
        f"Pending: {paise_to_rupees_str(snap.pending_paise)}\n\n"
        "You earn <b>8%</b> of a referred user's first 3 completed orders "
        "and <b>5%</b> of their first deposit. Credits land on the ledger "
        "and are idempotent."
    )


@router.callback_query(MenuCB.filter(F.a == "rewards"))
async def cb_rewards(
    query: CallbackQuery, session: AsyncSession, settings: Settings
) -> None:
    if query.from_user is None:
        return
    user = await ensure_user(session, query.from_user, settings)
    username = await resolve_bot_username(query, settings.bot_username)
    text = await _rewards_body(session, user, settings, username)
    text, markup = await apply_ad(session, "rewards", text, rewards_kb())
    await safe_edit(query, text, markup)


@router.message(Command("rewards"))
async def cmd_rewards(message: Message, session: AsyncSession, settings: Settings) -> None:
    if message.from_user is None:
        return
    user = await ensure_user(session, message.from_user, settings)
    username = await resolve_bot_username(message, settings.bot_username)
    text = await _rewards_body(session, user, settings, username)
    text, markup = await apply_ad(session, "rewards", text, rewards_kb())
    await message.answer(text, parse_mode="HTML", reply_markup=markup)


@router.callback_query(ShareCB.filter(F.v == "pick"))
async def cb_share_pick(
    query: CallbackQuery, callback_data: ShareCB, session: AsyncSession, settings: Settings
) -> None:
    if query.from_user is None:
        return
    user = await ensure_user(session, query.from_user, settings)
    from app.referrals import ensure_referral_code

    code = await ensure_referral_code(session, user)
    username = await resolve_bot_username(query, settings.bot_username)
    link = deep_link(username, code)
    await safe_edit(query, picker_text(callback_data.k, code, link), share_picker_kb(callback_data.k))


@router.callback_query(ShareCB.filter(F.v.in_({"1", "2", "3", "4"})))
async def cb_share_send(
    query: CallbackQuery, callback_data: ShareCB, session: AsyncSession, settings: Settings
) -> None:
    if query.from_user is None:
        return
    user = await ensure_user(session, query.from_user, settings)
    from app.referrals import ensure_referral_code

    code = await ensure_referral_code(session, user)
    username = await resolve_bot_username(query, settings.bot_username)
    link = deep_link(username, code)
    variants = captions_for(callback_data.k, code, link)
    try:
        idx = int(callback_data.v) - 1
    except ValueError:
        idx = 0
    caption = variants[idx] if 0 <= idx < len(variants) else variants[0]
    try:
        await query.answer("Sending card…")
    except Exception:  # noqa: BLE001
        pass
    await send_share_card(query, caption=caption, link=link)
