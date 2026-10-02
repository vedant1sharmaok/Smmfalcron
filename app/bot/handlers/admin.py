"""Owner-only admin: dashboard, kill switches, lookup, credit, providers, ads, applications."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ads import list_ads
from app.audit import audit
from app.bot.handlers.start import ensure_user, is_staff
from app.bot.menus import AdmCB, admin_back_kb, admin_home_kb, html_escape, safe_edit
from app.catalog import kill_switches, set_setting
from app.config import Settings
from app.models import Ad, KILL_MAINTENANCE, Order, Payment, Provider, RoleApplication, User
from app.pricing import paise_to_rupees_str
from app.security import new_idempotency_key, new_public_id
from app.referrals import approve_application, reject_application
from app.wallet import WalletError, credit, debit, snapshot_of
from app.notify import notify_user
from app.workers import health_check_providers_once

router = Router(name="admin")


class AdminCredit(StatesGroup):
    user = State()
    amount = State()
    reason = State()


def _require_staff(user: User, settings: Settings) -> bool:
    return is_staff(user, settings)


async def _flags(session: AsyncSession) -> dict[str, bool]:
    return await kill_switches(session)


async def _dashboard(session: AsyncSession) -> str:
    users = (await session.execute(select(func.count()).select_from(User))).scalar_one()
    orders = (await session.execute(select(func.count()).select_from(Order))).scalar_one()
    open_orders = (
        await session.execute(
            select(func.count()).select_from(Order).where(
                Order.status.in_(("pending", "awaiting_provider", "processing", "in_progress", "refilling"))
            )
        )
    ).scalar_one()
    volume = (
        await session.execute(
            select(func.coalesce(func.sum(Order.charge_paise), 0)).where(
                Order.status.notin_(("failed", "canceled", "cancelled"))
            )
        )
    ).scalar_one()
    flags = await _flags(session)
    flag_lines = "\n".join(f"• {k}: {'ON' if v else 'off'}" for k, v in flags.items())
    providers = list((await session.execute(select(Provider))).scalars().all())
    prov_lines = "\n".join(
        f"• {html_escape(p.name)} [{p.id}] {p.adapter_type} · {p.health_status}"
        for p in providers
    )
    pending_apps = int(
        (
            await session.execute(
                select(func.count()).select_from(RoleApplication).where(RoleApplication.status == "pending")
            )
        ).scalar_one()
    )
    return (
        "<b>Admin dashboard</b>\n\n"
        f"Users: {int(users)}\n"
        f"Orders: {int(orders)} (open {int(open_orders)})\n"
        f"Volume: {paise_to_rupees_str(int(volume))}\n"
        f"Pending applications: {pending_apps}\n\n"
        f"<b>Kill switches</b>\n{flag_lines}\n\n"
        f"<b>Providers</b>\n{prov_lines or '—'}"
    )


@router.message(Command("admin"))
async def cmd_admin(
    message: Message, session: AsyncSession, settings: Settings, state: FSMContext
) -> None:
    await state.clear()
    if message.from_user is None:
        return
    user = await ensure_user(session, message.from_user, settings)
    if not _require_staff(user, settings):
        await message.answer("Not authorised.")
        return
    await message.answer(
        "Admin control. Toggle switches below. Keys and secrets are never shown.",
        reply_markup=admin_home_kb(await _flags(session)),
        parse_mode="HTML",
    )


@router.callback_query(AdmCB.filter(F.a == "home"))
async def cb_admin_home(
    query: CallbackQuery, session: AsyncSession, settings: Settings, state: FSMContext
) -> None:
    await state.clear()
    if query.from_user is None:
        return
    user = await ensure_user(session, query.from_user, settings)
    if not _require_staff(user, settings):
        await query.answer("Not authorised", show_alert=True)
        return
    await safe_edit(
        query,
        "Admin control. Toggle switches below. Keys and secrets are never shown.",
        admin_home_kb(await _flags(session)),
    )


@router.callback_query(AdmCB.filter(F.a == "dash"))
async def cb_dash(
    query: CallbackQuery, session: AsyncSession, settings: Settings
) -> None:
    if query.from_user is None:
        return
    user = await ensure_user(session, query.from_user, settings)
    if not _require_staff(user, settings):
        await query.answer("Not authorised", show_alert=True)
        return
    await safe_edit(query, await _dashboard(session), admin_back_kb())


@router.callback_query(AdmCB.filter(F.a == "k"))
async def cb_toggle_kill(
    query: CallbackQuery, callback_data: AdmCB, session: AsyncSession, settings: Settings
) -> None:
    if query.from_user is None:
        return
    user = await ensure_user(session, query.from_user, settings)
    if not _require_staff(user, settings):
        await query.answer("Not authorised", show_alert=True)
        return
    key = callback_data.v
    allowed = {
        "kill_new_orders",
        "kill_payments",
        "kill_refills",
        "read_only",
        "global_maintenance",
    }
    if key not in allowed:
        await query.answer("Unknown switch", show_alert=True)
        return
    flags = await _flags(session)
    new_value = "0" if flags.get(key) else "1"
    await set_setting(session, key, new_value)
    await audit(session, actor=user.telegram_id, action="killswitch.toggle", target=key,
                old=("1" if flags.get(key) else "0"), new=new_value)
    await query.answer("Updated")
    await safe_edit(
        query,
        "Admin control. Toggle switches below. Keys and secrets are never shown.",
        admin_home_kb(await _flags(session)),
    )


@router.callback_query(AdmCB.filter(F.a == "prov"))
async def cb_providers(
    query: CallbackQuery, session: AsyncSession, settings: Settings
) -> None:
    if query.from_user is None:
        return
    user = await ensure_user(session, query.from_user, settings)
    if not _require_staff(user, settings):
        await query.answer("Not authorised", show_alert=True)
        return
    providers = list((await session.execute(select(Provider))).scalars().all())
    lines = ["<b>Providers</b>", "API keys are stored encrypted and are never sent to Telegram.", ""]
    for p in providers:
        checked = p.last_health_at.strftime("%H:%M:%S") if p.last_health_at else "never"
        lines.append(
            f"<b>{html_escape(p.name)}</b> ({html_escape(p.id)})\n"
            f"adapter={html_escape(p.adapter_type)} active={p.is_active}\n"
            f"health={html_escape(p.health_status)} last={checked}\n"
            f"detail={html_escape(p.last_health_detail or '—')}\n"
        )
    await safe_edit(query, "\n".join(lines), admin_back_kb())


@router.callback_query(AdmCB.filter(F.a == "health"))
async def cb_health(
    query: CallbackQuery, session: AsyncSession, settings: Settings
) -> None:
    if query.from_user is None:
        return
    user = await ensure_user(session, query.from_user, settings)
    if not _require_staff(user, settings):
        await query.answer("Not authorised", show_alert=True)
        return
    await query.answer("Running health checks…")
    # Commit current transaction first; the worker opens its own session.
    await session.commit()
    await health_check_providers_once()
    # Re-read
    providers = list((await session.execute(select(Provider))).scalars().all())
    lines = ["<b>Health check</b>"]
    for p in providers:
        lines.append(f"• {html_escape(p.name)}: {html_escape(p.health_status)} — {html_escape(p.last_health_detail or '')}")
    await safe_edit(query, "\n".join(lines), admin_back_kb())


@router.callback_query(AdmCB.filter(F.a == "lookup"))
async def cb_lookup_start(
    query: CallbackQuery, session: AsyncSession, settings: Settings, state: FSMContext
) -> None:
    if query.from_user is None:
        return
    user = await ensure_user(session, query.from_user, settings)
    if not _require_staff(user, settings):
        await query.answer("Not authorised", show_alert=True)
        return
    await state.set_state(AdminCredit.user)
    await state.update_data(mode="lookup")
    await safe_edit(query, "Send the Telegram numeric id or @username to look up.", admin_back_kb())


@router.callback_query(AdmCB.filter(F.a == "credit"))
async def cb_credit_start(
    query: CallbackQuery, session: AsyncSession, settings: Settings, state: FSMContext
) -> None:
    if query.from_user is None:
        return
    user = await ensure_user(session, query.from_user, settings)
    if not _require_staff(user, settings):
        await query.answer("Not authorised", show_alert=True)
        return
    await state.set_state(AdminCredit.user)
    await state.update_data(mode="credit")
    await safe_edit(
        query,
        "Send the Telegram numeric id of the user to credit or debit.",
        admin_back_kb(),
    )


async def _find_user(session: AsyncSession, raw: str) -> User | None:
    raw = raw.strip()
    if raw.startswith("@"):
        return (
            await session.execute(select(User).where(User.username == raw[1:]))
        ).scalar_one_or_none()
    try:
        tid = int(raw)
    except ValueError:
        return (
            await session.execute(select(User).where(User.username == raw))
        ).scalar_one_or_none()
    return await session.get(User, tid)


def _user_card(target: User) -> str:
    snap = snapshot_of(target)
    uname = f"@{target.username}" if target.username else "—"
    return (
        f"<b>{html_escape(target.display_name)}</b>\n"
        f"Telegram id: <code>{target.telegram_id}</code>\n"
        f"Username: {html_escape(uname)}\n"
        f"First name: {html_escape(target.first_name or '—')}\n"
        f"Last name: {html_escape(target.last_name or '—')}\n"
        f"Language: {html_escape(target.language_code or 'en')}\n"
        f"Role: {html_escape(target.role)}\n"
        f"Banned: {target.is_banned} · welcome bonus: {target.welcome_bonus_granted}\n"
        f"Referral code: <code>{html_escape(target.referral_code or '—')}</code>\n"
        f"Available: {paise_to_rupees_str(snap.available_paise)} "
        f"reserved: {paise_to_rupees_str(snap.reserved_paise)}"
    )


@router.message(AdminCredit.user)
async def admin_got_user(
    message: Message, session: AsyncSession, settings: Settings, state: FSMContext
) -> None:
    if message.from_user is None or not message.text:
        return
    actor = await ensure_user(session, message.from_user, settings)
    if not _require_staff(actor, settings):
        await state.clear()
        return
    target = await _find_user(session, message.text)
    if target is None:
        await message.answer("User not found. They must have opened the bot at least once.")
        return
    data = await state.get_data()
    if data.get("mode") == "lookup":
        await state.clear()
        await message.answer(_user_card(target), parse_mode="HTML", reply_markup=admin_back_kb())
        return
    await state.update_data(target_id=target.telegram_id)
    await state.set_state(AdminCredit.amount)
    await message.answer(
        _user_card(target)
        + "\n\nSend amount in INR (positive to credit, negative to debit). Example: 250 or -50",
        parse_mode="HTML",
    )


@router.message(AdminCredit.amount)
async def admin_got_amount(message: Message, state: FSMContext, session: AsyncSession, settings: Settings) -> None:
    if message.from_user is None or not message.text:
        return
    actor = await ensure_user(session, message.from_user, settings)
    if not _require_staff(actor, settings):
        await state.clear()
        return
    raw = message.text.replace(",", "").replace("₹", "").strip()
    try:
        rupees = float(raw)
    except ValueError:
        await message.answer("Send a number, e.g. 250 or -50.")
        return
    paise = int(round(rupees * 100))
    if paise == 0:
        await message.answer("Amount cannot be zero.")
        return
    await state.update_data(amount_paise=paise)
    await state.set_state(AdminCredit.reason)
    await message.answer("Reason is mandatory. Send a short note for the ledger.")


@router.message(AdminCredit.reason)
async def admin_got_reason(
    message: Message, session: AsyncSession, settings: Settings, state: FSMContext
) -> None:
    if message.from_user is None or not message.text:
        return
    actor = await ensure_user(session, message.from_user, settings)
    if not _require_staff(actor, settings):
        await state.clear()
        return
    reason = message.text.strip()
    if len(reason) < 3:
        await message.answer("Reason must be at least 3 characters.")
        return
    data = await state.get_data()
    target = await session.get(User, int(data["target_id"]))
    if target is None:
        await state.clear()
        await message.answer("User disappeared.")
        return
    amount = int(data["amount_paise"])
    try:
        if amount > 0:
            await credit(
                session,
                target,
                amount,
                reason=f"Admin: {reason}",
                idempotency_key=new_idempotency_key("admin-adj", new_public_id("ADJ")),
                reference_type="admin",
                reference_id=str(actor.telegram_id),
                created_by=actor.telegram_id,
            )
            verb = "Credited"
        else:
            await debit(
                session,
                target,
                abs(amount),
                reason=f"Admin: {reason}",
                idempotency_key=new_idempotency_key("admin-adj", new_public_id("ADJ")),
                reference_type="admin",
                reference_id=str(actor.telegram_id),
                created_by=actor.telegram_id,
            )
            verb = "Debited"
    except WalletError as exc:
        await message.answer(f"Wallet error: {exc}")
        await state.clear()
        return
    await state.clear()
    await audit(session, actor=actor.telegram_id, action=f"wallet.admin_{verb.lower()}", target=str(target.telegram_id),
                new={"amount_paise": amount}, reason=reason)
    snap = snapshot_of(target)
    await message.answer(
        f"{verb} {paise_to_rupees_str(abs(amount))} for {target.telegram_id}.\n"
        f"Reason: {html_escape(reason)}\n"
        f"New available: {paise_to_rupees_str(snap.available_paise)}",
        parse_mode="HTML",
        reply_markup=admin_back_kb(),
    )


@router.message(Command("maintenance"))
async def cmd_maintenance(
    message: Message, session: AsyncSession, settings: Settings
) -> None:
    if message.from_user is None:
        return
    user = await ensure_user(session, message.from_user, settings)
    if not _require_staff(user, settings):
        return
    flags = await _flags(session)
    new_value = "0" if flags.get(KILL_MAINTENANCE) else "1"
    await set_setting(session, KILL_MAINTENANCE, new_value)
    await message.answer("Maintenance " + ("ON" if new_value == "1" else "off"))


@router.message(Command("confirmpay"))
async def cmd_confirmpay(
    message: Message, command: CommandObject, session: AsyncSession, settings: Settings
) -> None:
    """/confirmpay PAY-XXXXXXXX — credit a MANUAL-gateway deposit after verifying the money arrived."""
    if message.from_user is None:
        return
    user = await ensure_user(session, message.from_user, settings)
    if not _require_staff(user, settings):
        return
    ref = (command.args or "").strip().upper()
    if not ref.startswith("PAY-"):
        await message.answer("Usage: /confirmpay PAY-XXXXXXXX")
        return
    from app.payments.service import manual_confirm

    payment = (await session.execute(select(Payment).where(Payment.public_id == ref))).scalar_one_or_none()
    if payment is None:
        await message.answer("Invoice not found.")
        return
    if payment.method != "manual":
        await message.answer("Only manual-gateway invoices can be confirmed by hand. Online payments settle automatically.")
        return
    outcome = await manual_confirm(session, ref, actor=str(user.telegram_id))
    await message.answer(f"{outcome.outcome}: {html_escape(ref)}", parse_mode="HTML")
    if outcome.outcome == "credited" and outcome.user_id is not None:
        await notify_user(
            outcome.user_id,
            f"Payment received: <b>{paise_to_rupees_str(outcome.amount_paise or 0)}</b> added to your wallet.",
        )


@router.message(Command("sync"))
async def cmd_sync(message: Message, session: AsyncSession, settings: Settings) -> None:
    """/sync — pull every provider's catalog now. New services arrive INACTIVE."""
    if message.from_user is None:
        return
    user = await ensure_user(session, message.from_user, settings)
    if not _require_staff(user, settings):
        return
    from app.sync import render_alert, sync_all

    await message.answer("Syncing provider catalogs…")
    reports = await sync_all(session)
    text = "\n".join(html_escape(r.summary()) for r in reports) or "No active providers."
    digest = render_alert(reports)
    await message.answer(text + (("\n\n" + digest) if digest else ""), parse_mode="HTML")


@router.message(Command("revieworders"))
async def cmd_review_orders(message: Message, session: AsyncSession, settings: Settings) -> None:
    """/revieworders — orders whose provider outcome is unknown (funds are held)."""
    if message.from_user is None:
        return
    user = await ensure_user(session, message.from_user, settings)
    if not _require_staff(user, settings):
        return
    from app.orders import list_review_orders

    rows = await list_review_orders(session, limit=20)
    if not rows:
        await message.answer("No orders under review.")
        return
    lines = ["<b>Orders under review</b> (funds held)"]
    for o in rows:
        lines.append(f"• <code>{html_escape(o.public_id)}</code> user {o.user_id} · {paise_to_rupees_str(o.charge_paise)}")
    lines.append("\nResolve on the server:\n<code>python -m app.cli resolve-order ID --release</code>\n"
                 "<code>python -m app.cli resolve-order ID --attach PROVIDER_ORDER_ID</code>")
    await message.answer("\n".join(lines), parse_mode="HTML")


def _ads_kb(ads: list[Ad]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for ad in ads:
        mark = "●" if ad.active else "○"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{mark} #{ad.id} {ad.placement} · {ad.title[:24]}",
                    callback_data=AdmCB(a="adt", v=str(ad.id)).pack(),
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="← Admin", callback_data=AdmCB(a="home").pack())])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(AdmCB.filter(F.a == "ads"))
async def cb_ads(query: CallbackQuery, session: AsyncSession, settings: Settings) -> None:
    if query.from_user is None:
        return
    user = await ensure_user(session, query.from_user, settings)
    if not _require_staff(user, settings):
        await query.answer("Not authorised", show_alert=True)
        return
    ads = await list_ads(session)
    if not ads:
        await safe_edit(query, "<b>Ads</b>\nNo ads seeded.", admin_back_kb())
        return
    lines = ["<b>Ads</b>", "Tap a row to toggle active.", ""]
    for ad in ads:
        state = "ON" if ad.active else "off"
        lines.append(
            f"#{ad.id} [{html_escape(ad.placement)}] {html_escape(ad.title)} · {state}\n"
            f"{html_escape(ad.caption)}"
        )
    await safe_edit(query, "\n\n".join(lines), _ads_kb(ads))


@router.callback_query(AdmCB.filter(F.a == "adt"))
async def cb_ads_toggle(
    query: CallbackQuery, callback_data: AdmCB, session: AsyncSession, settings: Settings
) -> None:
    if query.from_user is None:
        return
    user = await ensure_user(session, query.from_user, settings)
    if not _require_staff(user, settings):
        await query.answer("Not authorised", show_alert=True)
        return
    try:
        ad_id = int(callback_data.v)
    except ValueError:
        await query.answer("Bad id", show_alert=True)
        return
    ad = await session.get(Ad, ad_id)
    if ad is None:
        await query.answer("Not found", show_alert=True)
        return
    ad.active = not bool(ad.active)
    await query.answer("ON" if ad.active else "off")
    ads = await list_ads(session)
    lines = ["<b>Ads</b>", "Tap a row to toggle active.", ""]
    for row in ads:
        state = "ON" if row.active else "off"
        lines.append(
            f"#{row.id} [{html_escape(row.placement)}] {html_escape(row.title)} · {state}\n"
            f"{html_escape(row.caption)}"
        )
    await safe_edit(query, "\n\n".join(lines), _ads_kb(ads))


def _apps_kb(apps: list[RoleApplication]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for app in apps[:12]:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"✓ {app.kind} {app.user_id}",
                    callback_data=AdmCB(a="appr", v=str(app.id)).pack(),
                ),
                InlineKeyboardButton(
                    text="✗",
                    callback_data=AdmCB(a="arej", v=str(app.id)).pack(),
                ),
            ]
        )
    rows.append([InlineKeyboardButton(text="← Admin", callback_data=AdmCB(a="home").pack())])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(AdmCB.filter(F.a == "apps"))
async def cb_apps(query: CallbackQuery, session: AsyncSession, settings: Settings) -> None:
    if query.from_user is None:
        return
    user = await ensure_user(session, query.from_user, settings)
    if not _require_staff(user, settings):
        await query.answer("Not authorised", show_alert=True)
        return
    apps = list(
        (
            await session.execute(
                select(RoleApplication)
                .where(RoleApplication.status == "pending")
                .order_by(RoleApplication.id.desc())
                .limit(20)
            )
        ).scalars().all()
    )
    if not apps:
        await safe_edit(query, "<b>Applications</b>\nNone pending.", admin_back_kb())
        return
    lines = ["<b>Pending applications</b>", ""]
    for app in apps:
        lines.append(
            f"#{app.id} {html_escape(app.kind)} · user <code>{app.user_id}</code>\n"
            f"{html_escape((app.pitch or '')[:240])}"
        )
    await safe_edit(query, "\n\n".join(lines), _apps_kb(apps))


@router.callback_query(AdmCB.filter(F.a.in_({"appr", "arej"})))
async def cb_app_decide(
    query: CallbackQuery, callback_data: AdmCB, session: AsyncSession, settings: Settings
) -> None:
    if query.from_user is None:
        return
    user = await ensure_user(session, query.from_user, settings)
    if not _require_staff(user, settings):
        await query.answer("Not authorised", show_alert=True)
        return
    try:
        app_id = int(callback_data.v)
    except ValueError:
        await query.answer("Bad id", show_alert=True)
        return
    application = await session.get(RoleApplication, app_id)
    if application is None:
        await query.answer("Not found", show_alert=True)
        return
    if callback_data.a == "appr":
        await approve_application(session, application)
        await query.answer("Approved")
    else:
        await reject_application(session, application)
        await query.answer("Rejected")
    await cb_apps(query, session, settings)
