"""Profile, settings, support. Telegram identity only — never email."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from app.ads import apply_ad
from app.bot.handlers.start import ensure_user
from app.bot.menus import MenuCB, back_home_kb, html_escape, profile_kb, safe_edit, settings_kb
from app.catalog import get_setting
from app.config import Settings
from app.pricing import paise_to_rupees_str
from app.wallet import snapshot_of

router = Router(name="profile")


@router.callback_query(MenuCB.filter(F.a == "profile"))
async def cb_profile(
    query: CallbackQuery, session: AsyncSession, settings: Settings
) -> None:
    if query.from_user is None:
        return
    user = await ensure_user(session, query.from_user, settings)
    snap = snapshot_of(user)
    uname = f"@{user.username}" if user.username else "—"
    lang = user.language_code or "en"
    text = (
        "<b>Profile</b>\n\n"
        f"Name: {html_escape(user.display_name)}\n"
        f"Username: {html_escape(uname)}\n"
        f"Telegram id: <code>{user.telegram_id}</code>\n"
        f"Language: {html_escape(lang)}\n"
        f"Role: {html_escape(user.role)}\n"
        f"Wallet: {paise_to_rupees_str(snap.available_paise)}\n"
        f"Joined: {user.created_at.strftime('%Y-%m-%d') if user.created_at else '—'}"
    )
    text, markup = await apply_ad(session, "profile", text, profile_kb())
    await safe_edit(query, text, markup)


@router.callback_query(MenuCB.filter(F.a == "settings"))
async def cb_settings(
    query: CallbackQuery, session: AsyncSession, settings: Settings
) -> None:
    if query.from_user is None:
        return
    user = await ensure_user(session, query.from_user, settings)
    await safe_edit(
        query,
        "<b>Settings</b>\nToggle order notifications and review policies.",
        settings_kb(user.notify_orders),
    )


@router.callback_query(MenuCB.filter(F.a == "toggle_notify"))
async def cb_toggle_notify(
    query: CallbackQuery, session: AsyncSession, settings: Settings
) -> None:
    if query.from_user is None:
        return
    user = await ensure_user(session, query.from_user, settings)
    user.notify_orders = not bool(user.notify_orders)
    await query.answer("Saved")
    await safe_edit(
        query,
        "<b>Settings</b>\nToggle order notifications and review policies.",
        settings_kb(user.notify_orders),
    )


@router.callback_query(MenuCB.filter(F.a == "support"))
async def cb_support(query: CallbackQuery, session: AsyncSession) -> None:
    url = await get_setting(session, "support_url", "")
    extra = f"\n\n{html_escape(url)}" if url else ""
    await safe_edit(
        query,
        "<b>Support</b>\n\n"
        "Include your order id (FL-…) and a short description. "
        "Never share payment credentials or API keys in chat."
        f"{extra}",
        back_home_kb(),
    )
