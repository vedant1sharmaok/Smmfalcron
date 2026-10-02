"""Privacy/terms gate, main menu, shared policy pages, /start deep links."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.ads import apply_ad
from app.bot.menus import MenuCB, html_escape, main_menu_kb, safe_edit, terms_kb
from app.catalog import get_setting, kill_switches
from app.config import Settings
from app.models import SETTING_WELCOME_BONUS, User
from app.pricing import paise_to_rupees_str
from app.referrals import partner_flags, record_start_hit
from app.security import utcnow
from app.wallet import credit, get_or_create_user, snapshot_of

router = Router(name="start")


async def ensure_user(session: AsyncSession, telegram_user, settings: Settings) -> User:
    return await get_or_create_user(
        session,
        telegram_user.id,
        username=telegram_user.username,
        first_name=telegram_user.first_name,
        last_name=telegram_user.last_name,
        language_code=telegram_user.language_code,
        owner_id=settings.owner_telegram_id,
    )


def is_staff(user: User, settings: Settings) -> bool:
    return user.role in {"admin", "owner"} or user.telegram_id == settings.owner_telegram_id


async def menu_kb_for(session: AsyncSession, user: User, settings: Settings):
    res, pro = await partner_flags(session, user, settings.owner_telegram_id)
    return main_menu_kb(
        is_staff=is_staff(user, settings),
        is_reseller=res,
        is_promoter=pro,
        webapp_url=settings.webapp_url,
    )


async def terms_text(session: AsyncSession) -> str:
    brand = await get_setting(session, "brand_name", "FALARON")
    return (
        f"<b>{html_escape(brand)}</b>\n\n"
        "Before you continue, please confirm you have read the Terms of Service "
        "and Privacy Policy.\n\n"
        "• Prices, balances and permissions are always calculated on the server.\n"
        "• Orders are submitted to third-party providers. Delivery times are estimates.\n"
        "• Refunds follow the order status (failed / canceled / partial).\n"
        "• Do not promote illegal, hateful, or scraped personal data.\n\n"
        "Tap <b>I agree — continue</b> to enter the app."
    )


async def main_menu_text(session: AsyncSession, user: User, settings: Settings) -> str:
    flags = await kill_switches(session)
    snap = snapshot_of(user)
    brand = await get_setting(session, "brand_name", "FALARON")
    banner = ""
    if flags.get("global_maintenance"):
        banner = "\n\n<i>The platform is in maintenance. Browsing is available, new orders are paused.</i>"
    elif flags.get("read_only"):
        banner = "\n\n<i>Read-only mode is on. New orders and deposits are paused.</i>"
    elif flags.get("kill_new_orders"):
        banner = "\n\n<i>New orders are temporarily paused.</i>"
    name = html_escape(user.display_name)
    return (
        f"<b>{html_escape(brand)}</b>\n"
        f"Hello, {name}.\n\n"
        f"Available: <b>{paise_to_rupees_str(snap.available_paise)}</b>\n"
        f"Reserved: {paise_to_rupees_str(snap.reserved_paise)}\n"
        f"Pick a section below.{banner}"
    )


async def render_home(
    target: CallbackQuery | Message,
    session: AsyncSession,
    user: User,
    settings: Settings,
    extra: str = "",
) -> None:
    text = await main_menu_text(session, user, settings) + extra
    markup = await menu_kb_for(session, user, settings)
    text, markup = await apply_ad(session, "home", text, markup)
    await safe_edit(target, text, markup)


@router.message(CommandStart())
async def cmd_start(
    message: Message,
    session: AsyncSession,
    settings: Settings,
    state: FSMContext,
    command: CommandObject,
) -> None:
    await state.clear()
    if message.from_user is None:
        return
    user = await ensure_user(session, message.from_user, settings)
    payload = (command.args or "").strip()
    if payload:
        await record_start_hit(session, code=payload, visitor=user)
    if user.is_banned:
        await message.answer("This account is suspended. Contact support if you believe this is a mistake.")
        return
    if user.terms_accepted_at is None:
        await message.answer(await terms_text(session), reply_markup=terms_kb(), parse_mode="HTML")
        return
    await render_home(message, session, user, settings)


@router.callback_query(MenuCB.filter(F.a == "accept_terms"))
async def accept_terms(
    query: CallbackQuery, session: AsyncSession, settings: Settings
) -> None:
    if query.from_user is None:
        return
    user = await ensure_user(session, query.from_user, settings)
    if user.terms_accepted_at is None:
        now = utcnow()
        user.terms_accepted_at = now
        user.privacy_accepted_at = now
    bonus_note = ""
    if not user.welcome_bonus_granted:
        raw = await get_setting(session, SETTING_WELCOME_BONUS, str(settings.welcome_bonus_paise))
        try:
            bonus = int(raw)
        except ValueError:
            bonus = settings.welcome_bonus_paise
        if bonus > 0:
            await credit(
                session,
                user,
                bonus,
                reason="Welcome bonus",
                idempotency_key=f"welcome:{user.telegram_id}",
                reference_type="bonus",
                reference_id="welcome",
            )
            user.welcome_bonus_granted = True
            bonus_note = f"\n\nWelcome bonus credited: <b>{paise_to_rupees_str(bonus)}</b>."
    await render_home(query, session, user, settings, extra=bonus_note)


@router.callback_query(MenuCB.filter(F.a == "home"))
async def go_home(
    query: CallbackQuery, session: AsyncSession, settings: Settings, state: FSMContext
) -> None:
    await state.clear()
    if query.from_user is None:
        return
    user = await ensure_user(session, query.from_user, settings)
    if user.terms_accepted_at is None:
        await safe_edit(query, await terms_text(session), terms_kb())
        return
    await render_home(query, session, user, settings)


@router.callback_query(MenuCB.filter(F.a.in_({"terms", "privacy"})))
async def show_policy(query: CallbackQuery, callback_data: MenuCB, session: AsyncSession) -> None:
    key = "terms_url" if callback_data.a == "terms" else "privacy_url"
    title = "Terms of Service" if callback_data.a == "terms" else "Privacy Policy"
    url = await get_setting(session, key, "")
    extra = f"\n\nFull document: {html_escape(url)}" if url else ""
    body = (
        f"<b>{title}</b>\n\n"
        "FALARON processes orders you submit, stores a service snapshot with each order, "
        "and keeps an immutable wallet ledger of every credit and debit. "
        "We do not share provider API keys. Mini App clients are untrusted — "
        "the server re-validates Telegram initData and recomputes every price. "
        "Identity is Telegram-only (id, username, name, language, role)."
        f"{extra}"
    )
    from app.bot.menus import back_home_kb

    await safe_edit(query, body, back_home_kb())


@router.message(Command("menu"))
async def cmd_menu(
    message: Message,
    session: AsyncSession,
    settings: Settings,
    state: FSMContext,
    command: CommandObject | None = None,
) -> None:
    await state.clear()
    if message.from_user is None:
        return
    user = await ensure_user(session, message.from_user, settings)
    if user.terms_accepted_at is None:
        await message.answer(await terms_text(session), reply_markup=terms_kb(), parse_mode="HTML")
        return
    await render_home(message, session, user, settings)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "<b>FALARON</b>\n"
        "/start — open the app\n"
        "/menu — main menu\n"
        "/balance — wallet snapshot\n"
        "/orders — recent orders\n"
        "/stats — personal stats\n"
        "/rewards — referral share\n"
        "/admin — owner tools\n",
        parse_mode="HTML",
    )
