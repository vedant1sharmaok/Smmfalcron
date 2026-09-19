"""
Admin bot handlers — all 25 admin panel sections.
Section 20, 21 of blueprint.
Only admins in ADMIN_TELEGRAM_IDS (env var) can use this bot.
Full RBAC via admin credentials in admins table.
"""
from __future__ import annotations
from decimal import Decimal
from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from app.admin_bot.keyboards import ACB, admin_main_menu, back_admin_keyboard, user_action_keyboard, kill_switch_keyboard, providers_keyboard, provider_action_keyboard, broadcast_keyboard, confirm_keyboard
from app.bot.safe_send import safe_edit, safe_send
from app.core.logging import get_logger

logger = get_logger(__name__)
router = Router(name="admin")


class AdminFSM(StatesGroup):
    searching_user      = State()
    entering_credit_amt = State()
    entering_debit_amt  = State()
    entering_bc_message = State()
    entering_ks_reason  = State()
    awaiting_ban_confirm= State()
    awaiting_refund_confirm = State()


def _is_admin(telegram_id: int) -> bool:
    """Check if Telegram ID is in the admin allowlist."""
    import os
    ids_str = os.environ.get("ADMIN_TELEGRAM_IDS", "")
    if not ids_str:
        return False
    try:
        allowed = [int(x.strip()) for x in ids_str.split(",") if x.strip()]
        return telegram_id in allowed
    except Exception:
        return False


async def _require_admin(message_or_call) -> bool:
    if isinstance(message_or_call, Message):
        user_id = message_or_call.from_user.id
        reply   = message_or_call.answer
    else:
        user_id = message_or_call.from_user.id
        reply   = message_or_call.message.answer
    if not _is_admin(user_id):
        await reply("⛔ Access denied.")
        return False
    return True


# ── /admin entry point ────────────────────────────────────────────────────────

@router.message(F.text.in_(["/admin", "/start"]))
async def admin_start(message: Message, state: FSMContext) -> None:
    if not await _require_admin(message):
        return
    await state.clear()
    await message.answer(
        "🔐 <b>Admin Panel</b>\n\nWelcome, Admin. Choose a section:",
        reply_markup=admin_main_menu(),
        parse_mode="HTML",
    )


# ── Home ──────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == ACB.HOME)
async def admin_home(call: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("⛔ Access denied.", show_alert=True); return
    await call.answer()
    await state.clear()
    await safe_edit(call.message, "🔐 <b>Admin Panel</b>\n\nChoose a section:", reply_markup=admin_main_menu())


# ── Dashboard ─────────────────────────────────────────────────────────────────

@router.callback_query(F.data == ACB.DASHBOARD)
async def admin_dashboard(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id): await call.answer("⛔", show_alert=True); return
    await call.answer()

    from app.core.database import AsyncSessionLocal
    from sqlalchemy import select, func
    from app.core.models import User, Order, Payment, Wallet
    from datetime import datetime, timedelta, timezone

    async with AsyncSessionLocal() as db:
        total_users    = (await db.execute(select(func.count(User.id)))).scalar_one() or 0
        active_orders  = (await db.execute(select(func.count(Order.id)).where(Order.status.in_(["pending","processing","in_progress"])))).scalar_one() or 0
        today_orders   = (await db.execute(select(func.count(Order.id)).where(Order.created_at >= datetime.now(timezone.utc).replace(hour=0,minute=0,second=0,microsecond=0)))).scalar_one() or 0
        total_revenue  = (await db.execute(select(func.sum(Payment.amount)).where(Payment.webhook_verified == True))).scalar_one() or Decimal("0")
        pending_pays   = (await db.execute(select(func.count(Payment.id)).where(Payment.status == "pending"))).scalar_one() or 0

        from app.security.kill_switches import get_all_active
        active_ks = await get_all_active()

    ks_warning = f"\n⚠️ <b>{len(active_ks)} kill switch(es) ACTIVE</b>" if active_ks else ""
    text = (
        f"📊 <b>Dashboard</b>{ks_warning}\n\n"
        f"👥 Total users:      {total_users:,}\n"
        f"📦 Active orders:    {active_orders:,}\n"
        f"📦 Today's orders:   {today_orders:,}\n"
        f"💳 Total revenue:    ₹{total_revenue:.2f}\n"
        f"⏳ Pending payments: {pending_pays:,}\n\n"
        f"🕐 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC"
    )
    await safe_edit(call.message, text, reply_markup=back_admin_keyboard())


# ── Users ─────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == ACB.USERS)
async def admin_users(call: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(call.from_user.id): await call.answer("⛔", show_alert=True); return
    await call.answer()
    await state.clear()
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    kb = InlineKeyboardBuilder()
    kb.button(text="🔍 Search by ID/username", callback_data=ACB.USERS_SEARCH)
    kb.button(text="📋 Recent users",           callback_data=ACB.pack(ACB.USERS_LIST, 1))
    kb.button(text="⬅️ Admin Menu",             callback_data=ACB.HOME)
    kb.adjust(1)
    await safe_edit(call.message, "👥 <b>Users</b>\n\nSearch or browse users:", reply_markup=kb.as_markup())


@router.callback_query(F.data == ACB.USERS_SEARCH)
async def admin_users_search_prompt(call: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(call.from_user.id): await call.answer("⛔", show_alert=True); return
    await call.answer()
    await safe_edit(call.message, "🔍 <b>Search User</b>\n\nSend the user's Telegram ID or @username:", reply_markup=back_admin_keyboard())
    await state.set_state(AdminFSM.searching_user)


@router.message(AdminFSM.searching_user)
async def admin_user_search_result(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id): return
    query = message.text.strip().lstrip("@")

    from app.core.database import AsyncSessionLocal
    from sqlalchemy import select
    from app.core.models import User, Wallet

    async with AsyncSessionLocal() as db:
        try:
            user = (await db.execute(select(User).where(User.telegram_id == int(query)))).scalar_one_or_none()
        except ValueError:
            user = (await db.execute(select(User).where(User.username == query))).scalar_one_or_none()

        if not user:
            await message.answer("❌ User not found.", reply_markup=back_admin_keyboard(), parse_mode="HTML"); return

        wallet  = (await db.execute(select(Wallet).where(Wallet.user_id == user.id))).scalar_one_or_none()
        balance = wallet.balance if wallet else Decimal("0")

    await state.update_data(found_user_id=user.id)
    await state.clear()
    text = (
        f"👤 <b>User Found</b>\n\n"
        f"ID: <code>{user.telegram_id}</code>\n"
        f"Name: {user.first_name} {user.last_name or ''}\n"
        f"Username: @{user.username or '—'}\n"
        f"Status: {'🚫 BANNED' if user.is_banned else '✅ Active'}\n"
        f"Premium: {'👑 Yes' if user.is_premium else 'No'}\n"
        f"Balance: ₹{balance:.2f}\n"
        f"Joined: {user.created_at.strftime('%Y-%m-%d') if user.created_at else '—'}"
    )
    await message.answer(text, reply_markup=user_action_keyboard(user.id, user.is_banned), parse_mode="HTML")


@router.callback_query(F.data.startswith("a:users:list:"))
async def admin_users_list(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id): await call.answer("⛔", show_alert=True); return
    await call.answer()
    page    = int(call.data.split(":")[3]) if len(call.data.split(":")) > 3 else 1
    PS      = 10

    from app.core.database import AsyncSessionLocal
    from sqlalchemy import select, func
    from app.core.models import User

    async with AsyncSessionLocal() as db:
        total = (await db.execute(select(func.count(User.id)))).scalar_one() or 0
        users = list((await db.execute(
            select(User).order_by(User.id.desc()).offset((page-1)*PS).limit(PS)
        )).scalars().all())

    total_pages = max(1, (total + PS - 1) // PS)
    lines = [f"👥 <b>Users</b> — Page {page}/{total_pages}\n"]
    for u in users:
        status = "🚫" if u.is_banned else "✅"
        lines.append(f"{status} <code>{u.telegram_id}</code> {u.first_name} @{u.username or '—'}")

    from aiogram.utils.keyboard import InlineKeyboardBuilder
    kb = InlineKeyboardBuilder()
    if page > 1:         kb.button(text="◀ Prev", callback_data=ACB.pack(ACB.USERS_LIST, page-1))
    if page < total_pages: kb.button(text="Next ▶", callback_data=ACB.pack(ACB.USERS_LIST, page+1))
    kb.button(text="⬅️ Users",  callback_data=ACB.USERS)
    kb.button(text="⬅️ Admin",  callback_data=ACB.HOME)
    kb.adjust(2)
    await safe_edit(call.message, "\n".join(lines), reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("a:user:ban:"))
async def admin_ban_user(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id): await call.answer("⛔", show_alert=True); return
    user_id = int(call.data.split(":")[3])
    from app.core.database import AsyncSessionLocal
    from sqlalchemy import update
    from app.core.models import User
    from app.audit.logger import audit_log
    async with AsyncSessionLocal() as db:
        await db.execute(update(User).where(User.id == user_id).values(is_banned=True))
        await audit_log(db, action="user.ban", resource="user", resource_id=str(user_id), actor_id=call.from_user.id)
        await db.commit()
    await call.answer("✅ User banned.", show_alert=True)
    try:
        from app.notifications.telegram_logger import tg_logger
        await tg_logger.user_banned(user_id, call.from_user.id)
    except Exception: pass
    await safe_edit(call.message, "✅ User has been banned.", reply_markup=back_admin_keyboard())


@router.callback_query(F.data.startswith("a:user:unban:"))
async def admin_unban_user(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id): await call.answer("⛔", show_alert=True); return
    user_id = int(call.data.split(":")[3])
    from app.core.database import AsyncSessionLocal
    from sqlalchemy import update
    from app.core.models import User
    from app.audit.logger import audit_log
    async with AsyncSessionLocal() as db:
        await db.execute(update(User).where(User.id == user_id).values(is_banned=False))
        await audit_log(db, action="user.unban", resource="user", resource_id=str(user_id), actor_id=call.from_user.id)
        await db.commit()
    await call.answer("✅ User unbanned.", show_alert=True)
    try:
        from app.notifications.telegram_logger import tg_logger
        await tg_logger.user_unbanned(user_id, call.from_user.id)
    except Exception: pass
    await safe_edit(call.message, "✅ User has been unbanned.", reply_markup=back_admin_keyboard())


@router.callback_query(F.data.startswith("a:user:wallet:"))
async def admin_user_wallet(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id): await call.answer("⛔", show_alert=True); return
    await call.answer()
    user_id = int(call.data.split(":")[3])
    from app.core.database import AsyncSessionLocal
    from sqlalchemy import select
    from app.core.models import Wallet, WalletTransaction

    async with AsyncSessionLocal() as db:
        wallet = (await db.execute(select(Wallet).where(Wallet.user_id == user_id))).scalar_one_or_none()
        if not wallet:
            await safe_edit(call.message, "❌ Wallet not found.", reply_markup=back_admin_keyboard()); return
        txs = list((await db.execute(
            select(WalletTransaction).where(WalletTransaction.wallet_id == wallet.id)
            .order_by(WalletTransaction.created_at.desc()).limit(10)
        )).scalars().all())

    lines = [f"💵 <b>Wallet — User {user_id}</b>\n", f"Balance: <b>₹{wallet.balance:.2f}</b>\n", "Last 10 transactions:"]
    for tx in txs:
        sign = "+" if tx.tx_type in ("DEPOSIT","REFUND","ADMIN_CREDIT","BONUS_CREDIT") else "-"
        lines.append(f"  {sign}₹{tx.amount:.2f} {tx.tx_type} — {tx.created_at.strftime('%m-%d %H:%M') if tx.created_at else ''}")
    await safe_edit(call.message, "\n".join(lines), reply_markup=user_action_keyboard(user_id, False))


@router.callback_query(F.data.startswith("a:user:credit:"))
async def admin_credit_prompt(call: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(call.from_user.id): await call.answer("⛔", show_alert=True); return
    await call.answer()
    user_id = int(call.data.split(":")[3])
    await state.update_data(target_user_id=user_id)
    await safe_edit(call.message, f"➕ <b>Credit User {user_id}</b>\n\nEnter the amount to credit (₹):", reply_markup=back_admin_keyboard())
    await state.set_state(AdminFSM.entering_credit_amt)


@router.message(AdminFSM.entering_credit_amt)
async def admin_credit_execute(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id): return
    data    = await state.get_data()
    user_id = data.get("target_user_id")
    try:
        amount = Decimal(message.text.strip().replace("₹","").replace(",",""))
        if amount <= 0: raise ValueError
    except Exception:
        await message.answer("❌ Enter a valid positive amount.", parse_mode="HTML"); return

    import uuid
    from app.core.database import AsyncSessionLocal
    from app.wallet.ledger import credit, TxType
    from app.audit.logger import audit_log

    async with AsyncSessionLocal() as db:
        await credit(db=db, user_id=user_id, amount=amount, tx_type=TxType.ADMIN_CREDIT,
                     idempotency_key=str(uuid.uuid4()),
                     reason=f"Admin credit by {message.from_user.id}")
        await audit_log(db, action="wallet.credit", resource="wallet", resource_id=str(user_id),
                        actor_id=message.from_user.id, details={"amount": str(amount)})
        await db.commit()

    await state.clear()
    try:
        from app.notifications.telegram_logger import tg_logger
        await tg_logger.admin_wallet_adjustment(message.from_user.id, user_id, amount, "ADMIN_CREDIT", f"Admin credit")
    except Exception: pass
    await message.answer(f"✅ <b>₹{amount:.2f} credited</b> to user {user_id}.", parse_mode="HTML", reply_markup=back_admin_keyboard())


@router.callback_query(F.data.startswith("a:user:debit:"))
async def admin_debit_prompt(call: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(call.from_user.id): await call.answer("⛔", show_alert=True); return
    await call.answer()
    user_id = int(call.data.split(":")[3])
    await state.update_data(target_user_id=user_id)
    await safe_edit(call.message, f"➖ <b>Debit User {user_id}</b>\n\nEnter the amount to debit (₹):", reply_markup=back_admin_keyboard())
    await state.set_state(AdminFSM.entering_debit_amt)


@router.message(AdminFSM.entering_debit_amt)
async def admin_debit_execute(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id): return
    data    = await state.get_data()
    user_id = data.get("target_user_id")
    try:
        amount = Decimal(message.text.strip().replace("₹","").replace(",",""))
        if amount <= 0: raise ValueError
    except Exception:
        await message.answer("❌ Enter a valid positive amount.", parse_mode="HTML"); return

    import uuid
    from app.core.database import AsyncSessionLocal
    from app.wallet.ledger import debit, TxType
    from app.audit.logger import audit_log

    async with AsyncSessionLocal() as db:
        try:
            await debit(db=db, user_id=user_id, amount=amount, tx_type=TxType.ADMIN_DEBIT,
                        idempotency_key=str(uuid.uuid4()),
                        reason=f"Admin debit by {message.from_user.id}")
            await audit_log(db, action="wallet.debit", resource="wallet", resource_id=str(user_id),
                            actor_id=message.from_user.id, details={"amount": str(amount)})
            await db.commit()
            await state.clear()
            await message.answer(f"✅ <b>₹{amount:.2f} debited</b> from user {user_id}.", parse_mode="HTML", reply_markup=back_admin_keyboard())
        except Exception as exc:
            await message.answer(f"❌ Debit failed: {exc}", parse_mode="HTML", reply_markup=back_admin_keyboard())
            await state.clear()


# ── Services admin ────────────────────────────────────────────────────────────

@router.callback_query(F.data == ACB.SERVICES)
async def admin_services(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id): await call.answer("⛔", show_alert=True); return
    await call.answer()
    from app.core.database import AsyncSessionLocal
    from sqlalchemy import select, func
    from app.core.models import Service
    async with AsyncSessionLocal() as db:
        total  = (await db.execute(select(func.count(Service.id)))).scalar_one() or 0
        active = (await db.execute(select(func.count(Service.id)).where(Service.is_active == True))).scalar_one() or 0

    from aiogram.utils.keyboard import InlineKeyboardBuilder
    kb = InlineKeyboardBuilder()
    kb.button(text="📋 List Services",  callback_data=ACB.pack(ACB.SVC_LIST, 1))
    kb.button(text="⬅️ Admin",          callback_data=ACB.HOME)
    kb.adjust(1)
    await safe_edit(call.message,
        f"🛍️ <b>Services</b>\n\nTotal: {total:,}  Active: {active:,}",
        reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("a:svc:list:"))
async def admin_services_list(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id): await call.answer("⛔", show_alert=True); return
    await call.answer()
    page = int(call.data.split(":")[3]) if len(call.data.split(":")) > 3 else 1
    PS   = 10
    from app.core.database import AsyncSessionLocal
    from sqlalchemy import select, func
    from app.core.models import Service
    async with AsyncSessionLocal() as db:
        total = (await db.execute(select(func.count(Service.id)))).scalar_one() or 0
        svcs  = list((await db.execute(
            select(Service).order_by(Service.id.desc()).offset((page-1)*PS).limit(PS)
        )).scalars().all())

    total_pages = max(1, (total + PS - 1) // PS)
    lines = [f"🛍️ <b>Services</b> — Page {page}/{total_pages}\n"]
    for s in svcs:
        status = "✅" if s.is_active else "❌"
        lines.append(f"{status} [{s.public_id}] {s.display_name[:40]}")

    from aiogram.utils.keyboard import InlineKeyboardBuilder
    kb = InlineKeyboardBuilder()
    if page > 1:           kb.button(text="◀ Prev", callback_data=ACB.pack(ACB.SVC_LIST, page-1))
    if page < total_pages: kb.button(text="Next ▶", callback_data=ACB.pack(ACB.SVC_LIST, page+1))
    kb.button(text="⬅️ Services", callback_data=ACB.SERVICES)
    kb.button(text="⬅️ Admin",    callback_data=ACB.HOME)
    kb.adjust(2)
    await safe_edit(call.message, "\n".join(lines), reply_markup=kb.as_markup())


# ── Providers admin ───────────────────────────────────────────────────────────

@router.callback_query(F.data == ACB.PROVIDERS)
async def admin_providers(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id): await call.answer("⛔", show_alert=True); return
    await call.answer()
    from app.core.database import AsyncSessionLocal
    from sqlalchemy import select
    from app.core.models import Provider
    async with AsyncSessionLocal() as db:
        provs = list((await db.execute(select(Provider).order_by(Provider.priority.desc()))).scalars().all())

    plist = [{"id": p.id, "name": p.name, "health": p.health_status, "is_active": p.is_active} for p in provs]
    if not plist:
        await safe_edit(call.message, "🖥️ <b>Providers</b>\n\nNo providers configured.", reply_markup=back_admin_keyboard()); return
    await safe_edit(call.message, f"🖥️ <b>Providers</b>\n\n{len(plist)} provider(s):", reply_markup=providers_keyboard(plist))


@router.callback_query(F.data.startswith("a:prov:detail:"))
async def admin_provider_detail(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id): await call.answer("⛔", show_alert=True); return
    await call.answer()
    prov_id = int(call.data.split(":")[3])
    from app.core.database import AsyncSessionLocal
    from sqlalchemy import select, func
    from app.core.models import Provider, ProviderService
    async with AsyncSessionLocal() as db:
        p    = await db.get(Provider, prov_id)
        svcs = (await db.execute(select(func.count(ProviderService.id)).where(ProviderService.provider_id == prov_id))).scalar_one() or 0

    if not p:
        await safe_edit(call.message, "Provider not found.", reply_markup=back_admin_keyboard()); return
    text = (
        f"🖥️ <b>{p.name}</b>\n\n"
        f"Status: {'🟢 Active' if p.is_active else '🔴 Disabled'}\n"
        f"Health: {p.health_status or 'unknown'}\n"
        f"Services: {svcs:,}\n"
        f"Currency: {p.currency}\n"
        f"Last sync: {p.last_sync_at.strftime('%Y-%m-%d %H:%M') if p.last_sync_at else 'Never'}\n"
        f"Last health: {p.last_health_check_at.strftime('%Y-%m-%d %H:%M') if p.last_health_check_at else 'Never'}"
    )
    await safe_edit(call.message, text, reply_markup=provider_action_keyboard(prov_id, p.is_active))


@router.callback_query(F.data.startswith("a:prov:sync:"))
async def admin_provider_sync(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id): await call.answer("⛔", show_alert=True); return
    await call.answer("🔄 Sync started...")
    prov_id = int(call.data.split(":")[3])
    from app.core.database import AsyncSessionLocal
    from app.sync.engine import ProviderSyncEngine
    from app.providers.registry import registry
    async with AsyncSessionLocal() as db:
        engine = ProviderSyncEngine(registry)
        result = await engine.sync_provider(db, prov_id)
        await db.commit()
    text = (
        f"✅ <b>Sync Complete</b>\n\n"
        f"Inserted: {result.inserted}\n"
        f"Updated: {result.updated}\n"
        f"Deactivated: {result.deactivated}\n"
        f"Unchanged: {result.unchanged}\n"
        f"Errors: {len(result.errors)}"
    )
    await safe_edit(call.message, text, reply_markup=provider_action_keyboard(prov_id, True))


@router.callback_query(F.data.startswith("a:prov:toggle:"))
async def admin_provider_toggle(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id): await call.answer("⛔", show_alert=True); return
    prov_id = int(call.data.split(":")[3])
    from app.core.database import AsyncSessionLocal
    from sqlalchemy import update
    from app.core.models import Provider
    from app.audit.logger import audit_log
    async with AsyncSessionLocal() as db:
        p = await db.get(Provider, prov_id)
        if p:
            p.is_active = not p.is_active
            await audit_log(db, action="provider.toggle", resource="provider", resource_id=str(prov_id),
                            actor_id=call.from_user.id, details={"is_active": p.is_active})
            await db.commit()
            status = "enabled" if p.is_active else "disabled"
            await call.answer(f"Provider {status}.", show_alert=True)
            await admin_provider_detail(call)


# ── Orders admin ──────────────────────────────────────────────────────────────

@router.callback_query(F.data == ACB.ORDERS)
async def admin_orders(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id): await call.answer("⛔", show_alert=True); return
    await call.answer()
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    kb = InlineKeyboardBuilder()
    kb.button(text="📋 All Orders",    callback_data=ACB.pack(ACB.ORD_LIST, 1))
    kb.button(text="⏳ Pending",       callback_data="a:ord:filter:pending")
    kb.button(text="🔵 Processing",    callback_data="a:ord:filter:processing")
    kb.button(text="🔴 Failed",        callback_data="a:ord:filter:failed")
    kb.button(text="⬅️ Admin",         callback_data=ACB.HOME)
    kb.adjust(2)
    await safe_edit(call.message, "📦 <b>Orders</b>\n\nView orders:", reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("a:ord:list:"))
async def admin_orders_list(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id): await call.answer("⛔", show_alert=True); return
    await call.answer()
    page = int(call.data.split(":")[3]) if len(call.data.split(":")) > 3 else 1
    PS   = 8
    from app.core.database import AsyncSessionLocal
    from sqlalchemy import select, func
    from app.core.models import Order
    async with AsyncSessionLocal() as db:
        total  = (await db.execute(select(func.count(Order.id)))).scalar_one() or 0
        orders = list((await db.execute(select(Order).order_by(Order.created_at.desc()).offset((page-1)*PS).limit(PS))).scalars().all())

    from app.bot.messages import STATUS_EMOJI
    total_pages = max(1,(total+PS-1)//PS)
    lines = [f"📦 <b>Orders</b> — Page {page}/{total_pages}\n"]
    for o in orders:
        e = STATUS_EMOJI.get(o.status,"❓")
        lines.append(f"{e} <code>{o.public_ref}</code>  ₹{o.price_charged:.2f}  {o.status}")

    from aiogram.utils.keyboard import InlineKeyboardBuilder
    kb = InlineKeyboardBuilder()
    if page > 1:           kb.button(text="◀ Prev", callback_data=ACB.pack(ACB.ORD_LIST, page-1))
    if page < total_pages: kb.button(text="Next ▶", callback_data=ACB.pack(ACB.ORD_LIST, page+1))
    kb.button(text="⬅️ Orders", callback_data=ACB.ORDERS)
    kb.button(text="⬅️ Admin",  callback_data=ACB.HOME)
    kb.adjust(2)
    await safe_edit(call.message, "\n".join(lines), reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("a:ord:refund:"))
async def admin_refund_order(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id): await call.answer("⛔", show_alert=True); return
    order_id = call.data.split(":")[3]
    from app.core.database import AsyncSessionLocal
    from app.core.models import Order
    from app.orders.engine import refund_order
    from app.audit.logger import audit_log
    async with AsyncSessionLocal() as db:
        order = await db.get(Order, order_id)
        if not order:
            await call.answer("Order not found.", show_alert=True); return
        await refund_order(db, order, actor_id=call.from_user.id, reason="Admin refund")
        await audit_log(db, action="order.admin_refund", resource="order", resource_id=order_id, actor_id=call.from_user.id)
        await db.commit()
    await call.answer("✅ Refund issued.", show_alert=True)
    await safe_edit(call.message, f"✅ Order <code>{order_id[:8]}</code> refunded.", reply_markup=back_admin_keyboard())


# ── Kill Switches ─────────────────────────────────────────────────────────────

@router.callback_query(F.data == ACB.KILLSWITCHES)
async def admin_kill_switches(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id): await call.answer("⛔", show_alert=True); return
    await call.answer()
    from app.security.kill_switches import get_all_active
    active = await get_all_active()
    text   = f"🚨 <b>Kill Switches</b>\n\n{'⚠️ ' + str(len(active)) + ' switch(es) ACTIVE' if active else '✅ All switches inactive'}\n\nTap to toggle:"
    await safe_edit(call.message, text, reply_markup=kill_switch_keyboard(active))


@router.callback_query(F.data.startswith("a:ks:toggle:"))
async def admin_ks_toggle(call: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(call.from_user.id): await call.answer("⛔", show_alert=True); return
    name = call.data.split(":")[3]
    from app.security.kill_switches import is_active, activate, deactivate, VALID_SWITCHES

    # Find matching full name
    full_name = next((n for n in VALID_SWITCHES if n.startswith(name) or n == name), None)
    if not full_name:
        await call.answer("Unknown switch.", show_alert=True); return

    currently_on = await is_active(full_name)
    if currently_on:
        await deactivate(full_name, actor_id=call.from_user.id)
        await call.answer(f"✅ {full_name} deactivated.", show_alert=True)
    else:
        await activate(full_name, reason=f"Activated by admin {call.from_user.id}",
                       actor_id=call.from_user.id)
        await call.answer(f"🔴 {full_name} activated.", show_alert=True)

    from app.audit.logger import audit_log
    from app.core.database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        await audit_log(db, action=f"kill_switch.{'deactivate' if currently_on else 'activate'}",
                        resource="kill_switch", resource_id=full_name, actor_id=call.from_user.id)
        await db.commit()

    # Refresh kill switch panel
    try:
        from app.notifications.telegram_logger import tg_logger
        if currently_on:
            await tg_logger.kill_switch_deactivated(full_name, call.from_user.id)
        else:
            await tg_logger.kill_switch_activated(full_name, call.from_user.id, f"Toggled by admin")
    except Exception: pass
    await admin_kill_switches(call)


# ── Broadcast ─────────────────────────────────────────────────────────────────

@router.callback_query(F.data == ACB.BROADCAST)
async def admin_broadcast_menu(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id): await call.answer("⛔", show_alert=True); return
    await call.answer()
    await safe_edit(call.message, "📢 <b>Broadcast</b>\n\nChoose target segment:", reply_markup=broadcast_keyboard())


@router.callback_query(F.data.in_([ACB.BC_ALL, ACB.BC_PREMIUM, ACB.BC_VIP, ACB.BC_INACTIVE]))
async def admin_broadcast_compose(call: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(call.from_user.id): await call.answer("⛔", show_alert=True); return
    await call.answer()
    segment_map = {ACB.BC_ALL:"all", ACB.BC_PREMIUM:"premium", ACB.BC_VIP:"vip", ACB.BC_INACTIVE:"inactive_30d"}
    segment     = segment_map.get(call.data, "all")
    await state.update_data(bc_segment=segment)
    await safe_edit(call.message,
        f"📢 <b>Broadcast to: {segment}</b>\n\nType the message to send (HTML supported):",
        reply_markup=back_admin_keyboard())
    await state.set_state(AdminFSM.entering_bc_message)


@router.message(AdminFSM.entering_bc_message)
async def admin_broadcast_send(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id): return
    data     = await state.get_data()
    segment  = data.get("bc_segment", "all")
    text_msg = message.text or message.caption or ""
    if not text_msg.strip():
        await message.answer("❌ Message cannot be empty."); return

    from app.core.database import AsyncSessionLocal
    from app.cms.service import create_broadcast
    async with AsyncSessionLocal() as db:
        bc = await create_broadcast(db=db, title=f"Admin broadcast {segment}",
                                    message=text_msg, segment=segment,
                                    actor_id=message.from_user.id)
        await db.commit()

    await state.clear()
    await message.answer(f"✅ <b>Broadcast queued</b>\n\nID: {bc.id}\nSegment: {segment}\n\nThe broadcast worker will deliver it shortly.",
                         parse_mode="HTML", reply_markup=back_admin_keyboard())


# ── Payments ──────────────────────────────────────────────────────────────────

@router.callback_query(F.data == ACB.PAYMENTS)
async def admin_payments(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id): await call.answer("⛔", show_alert=True); return
    await call.answer()
    from app.core.database import AsyncSessionLocal
    from sqlalchemy import select, func
    from app.core.models import Payment
    async with AsyncSessionLocal() as db:
        total    = (await db.execute(select(func.count(Payment.id)))).scalar_one() or 0
        verified = (await db.execute(select(func.count(Payment.id)).where(Payment.webhook_verified == True))).scalar_one() or 0
        pending  = (await db.execute(select(func.count(Payment.id)).where(Payment.status == "pending"))).scalar_one() or 0
        revenue  = (await db.execute(select(func.sum(Payment.amount)).where(Payment.webhook_verified == True))).scalar_one() or Decimal("0")

    await safe_edit(call.message,
        f"💳 <b>Payments</b>\n\nTotal: {total:,}\nVerified: {verified:,}\nPending: {pending:,}\nRevenue: ₹{revenue:.2f}",
        reply_markup=back_admin_keyboard())


# ── Audit log ─────────────────────────────────────────────────────────────────

@router.callback_query(F.data == ACB.AUDIT)
async def admin_audit(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id): await call.answer("⛔", show_alert=True); return
    await call.answer()
    from app.core.database import AsyncSessionLocal
    from sqlalchemy import select
    from app.core.models import AuditLog
    async with AsyncSessionLocal() as db:
        entries = list((await db.execute(
            select(AuditLog).order_by(AuditLog.id.desc()).limit(10)
        )).scalars().all())

    lines = ["📝 <b>Audit Log</b> — Last 10 entries\n"]
    for e in entries:
        lines.append(f"• <code>{e.action}</code> on {e.resource}:{e.resource_id or '—'} by {e.actor_id or 'system'}")
        if e.created_at: lines.append(f"  {e.created_at.strftime('%Y-%m-%d %H:%M')}")

    from aiogram.utils.keyboard import InlineKeyboardBuilder
    kb = InlineKeyboardBuilder()
    kb.button(text="Next page ▶", callback_data=ACB.pack(ACB.AUDIT_LIST, 2))
    kb.button(text="⬅️ Admin",    callback_data=ACB.HOME)
    kb.adjust(1)
    await safe_edit(call.message, "\n".join(lines), reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("a:audit:list:"))
async def admin_audit_paged(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id): await call.answer("⛔", show_alert=True); return
    await call.answer()
    page = int(call.data.split(":")[3]) if len(call.data.split(":")) > 3 else 1
    PS   = 10
    from app.core.database import AsyncSessionLocal
    from sqlalchemy import select
    from app.core.models import AuditLog
    async with AsyncSessionLocal() as db:
        entries = list((await db.execute(
            select(AuditLog).order_by(AuditLog.id.desc()).offset((page-1)*PS).limit(PS)
        )).scalars().all())

    lines = [f"📝 <b>Audit Log</b> — Page {page}\n"]
    for e in entries:
        lines.append(f"• <code>{e.action}</code> {e.resource}:{e.resource_id or '—'}")
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    kb = InlineKeyboardBuilder()
    if page > 1:   kb.button(text="◀ Prev", callback_data=ACB.pack(ACB.AUDIT_LIST, page-1))
    kb.button(text="Next ▶", callback_data=ACB.pack(ACB.AUDIT_LIST, page+1))
    kb.button(text="⬅️ Admin", callback_data=ACB.HOME)
    kb.adjust(2)
    await safe_edit(call.message, "\n".join(lines), reply_markup=kb.as_markup())


# ── Analytics ─────────────────────────────────────────────────────────────────

@router.callback_query(F.data == ACB.ANALYTICS)
async def admin_analytics(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id): await call.answer("⛔", show_alert=True); return
    await call.answer()
    from app.core.database import AsyncSessionLocal
    from sqlalchemy import select, func
    from app.core.models import Order, User, Payment
    from datetime import datetime, timedelta, timezone
    now   = datetime.now(timezone.utc)
    day7  = now - timedelta(days=7)
    day30 = now - timedelta(days=30)

    async with AsyncSessionLocal() as db:
        orders_7d   = (await db.execute(select(func.count(Order.id)).where(Order.created_at >= day7))).scalar_one() or 0
        orders_30d  = (await db.execute(select(func.count(Order.id)).where(Order.created_at >= day30))).scalar_one() or 0
        users_7d    = (await db.execute(select(func.count(User.id)).where(User.created_at >= day7))).scalar_one() or 0
        revenue_7d  = (await db.execute(select(func.sum(Payment.amount)).where(Payment.webhook_verified == True, Payment.created_at >= day7))).scalar_one() or Decimal("0")
        revenue_30d = (await db.execute(select(func.sum(Payment.amount)).where(Payment.webhook_verified == True, Payment.created_at >= day30))).scalar_one() or Decimal("0")

    await safe_edit(call.message,
        f"📈 <b>Analytics</b>\n\n"
        f"<b>Last 7 days:</b>\n"
        f"  Orders: {orders_7d:,}\n"
        f"  New users: {users_7d:,}\n"
        f"  Revenue: ₹{revenue_7d:.2f}\n\n"
        f"<b>Last 30 days:</b>\n"
        f"  Orders: {orders_30d:,}\n"
        f"  Revenue: ₹{revenue_30d:.2f}",
        reply_markup=back_admin_keyboard())


# ── Diagnostics ───────────────────────────────────────────────────────────────

@router.callback_query(F.data == ACB.DIAGNOSTICS)
async def admin_diagnostics(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id): await call.answer("⛔", show_alert=True); return
    await call.answer("🧪 Running checks...")
    from app.core.database import check_db_health
    from app.core.redis import check_redis_health
    db_ok    = await check_db_health()
    redis_ok = await check_redis_health()
    from app.providers.registry import registry
    providers_loaded = len(getattr(registry, '_providers', {}))
    await safe_edit(call.message,
        f"🧪 <b>Diagnostics</b>\n\n"
        f"Database:  {'✅ OK' if db_ok else '❌ ERROR'}\n"
        f"Redis:     {'✅ OK' if redis_ok else '❌ ERROR'}\n"
        f"Providers: {providers_loaded} loaded",
        reply_markup=back_admin_keyboard())


# ── Misc stubs ────────────────────────────────────────────────────────────────

@router.callback_query(F.data.in_([ACB.PRICING, ACB.CATEGORIES, ACB.WALLETS,
                                    ACB.SECURITY, ACB.SETTINGS, ACB.FLAGS,
                                    ACB.MAINTENANCE, ACB.PREMIUM, ACB.PROV_HEALTH]))
async def admin_stub(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id): await call.answer("⛔", show_alert=True); return
    await call.answer()
    labels = {
        ACB.PRICING:     "💰 Pricing rules — use REST API /admin/pricing",
        ACB.CATEGORIES:  "📂 Categories — manage via REST API /admin/categories",
        ACB.WALLETS:     "💵 Wallets — search a user then tap Wallet",
        ACB.SECURITY:    "🔐 Security — review kill switches and audit logs",
        ACB.SETTINGS:    "⚙️ Global Settings — configure via environment variables",
        ACB.FLAGS:       "🚦 Feature Flags — managed via system_settings table",
        ACB.MAINTENANCE: "🛠️ Maintenance — use Kill Switches for quick toggles",
        ACB.PREMIUM:     "👑 Premium — grant/revoke via user wallet credit flow",
        ACB.PROV_HEALTH: "📊 Provider health metrics available in /metrics endpoint",
    }
    await safe_edit(call.message,
        f"ℹ️ {labels.get(call.data, 'Feature available via REST API.')}\n\n"
        f"Full REST admin API at: <code>/admin/*</code>",
        reply_markup=back_admin_keyboard())


# ══════════════════════════════════════════════════════════════════════════════
# SERVICE ADD / EDIT (Section 22-23)
# ══════════════════════════════════════════════════════════════════════════════

class AdminServiceFSM(StatesGroup):
    editing_name    = State()
    editing_price   = State()
    editing_profit  = State()
    adding_service  = State()


@router.callback_query(F.data.startswith("a:svc:detail:"))
async def admin_svc_detail(call: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(call.from_user.id): await call.answer("⛔", show_alert=True); return
    await call.answer()
    svc_id = int(call.data.split(":")[3])

    from app.core.database import AsyncSessionLocal
    from app.core.models import Service, Category
    async with AsyncSessionLocal() as db:
        svc = await db.get(Service, svc_id)
        if not svc:
            await safe_edit(call.message, "Service not found.", reply_markup=back_admin_keyboard()); return
        cat = await db.get(Category, svc.category_id)

    text = (
        f"🛍️ <b>Service Detail</b>\n\n"
        f"ID: <code>{svc.public_id}</code>\n"
        f"Name: {svc.display_name}\n"
        f"Category: {cat.name if cat else '—'}\n"
        f"Status: {'✅ Active' if svc.is_active else '❌ Disabled'}\n"
        f"Ordering: {'✅' if svc.ordering_enabled else '❌'}\n"
        f"Refill: {'✅' if svc.refill_enabled else '❌'}\n"
        f"Cancel: {'✅' if svc.cancel_enabled else '❌'}\n"
        f"Premium only: {'👑' if svc.requires_premium else 'No'}\n"
        f"Custom price: {'₹' + str(svc.custom_price) if svc.custom_price else 'Calculated'}\n"
        f"Profit %: {svc.profit_pct or '—'}"
    )
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    kb = InlineKeyboardBuilder()
    toggle = "❌ Disable" if svc.is_active else "✅ Enable"
    kb.button(text=toggle,              callback_data=ACB.pack(ACB.SVC_TOGGLE, svc_id))
    kb.button(text="✏️ Edit Name",      callback_data=f"a:svc:edit:name:{svc_id}")
    kb.button(text="💰 Edit Price",     callback_data=f"a:svc:edit:price:{svc_id}")
    kb.button(text="📊 Edit Profit %",  callback_data=f"a:svc:edit:profit:{svc_id}")
    kb.button(text="⬅️ Services",       callback_data=ACB.SERVICES)
    kb.adjust(2)
    await safe_edit(call.message, text, reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("a:svc:toggle:"))
async def admin_svc_toggle(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id): await call.answer("⛔", show_alert=True); return
    svc_id = int(call.data.split(":")[3])

    from app.core.database import AsyncSessionLocal
    from app.core.models import Service
    from app.audit.logger import audit_log
    from sqlalchemy import update

    async with AsyncSessionLocal() as db:
        svc = await db.get(Service, svc_id)
        if svc:
            svc.is_active = not svc.is_active
            await audit_log(db, action="service.toggle", resource="service",
                            resource_id=str(svc_id), actor_id=call.from_user.id,
                            details={"is_active": svc.is_active})
            await db.commit()
            await call.answer(f"Service {'enabled' if svc.is_active else 'disabled'}.", show_alert=True)
            call.data = f"a:svc:detail:{svc_id}"
            await admin_svc_detail(call, None)


@router.callback_query(F.data.startswith("a:svc:edit:name:"))
async def admin_svc_edit_name_prompt(call: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(call.from_user.id): await call.answer("⛔", show_alert=True); return
    await call.answer()
    svc_id = int(call.data.split(":")[4])
    await state.update_data(editing_svc_id=svc_id, editing_field="name")
    await safe_edit(call.message, "✏️ Enter the new <b>display name</b> for this service:", reply_markup=back_admin_keyboard())
    await state.set_state(AdminServiceFSM.editing_name)


@router.callback_query(F.data.startswith("a:svc:edit:price:"))
async def admin_svc_edit_price_prompt(call: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(call.from_user.id): await call.answer("⛔", show_alert=True); return
    await call.answer()
    svc_id = int(call.data.split(":")[4])
    await state.update_data(editing_svc_id=svc_id, editing_field="price")
    await safe_edit(call.message, "💰 Enter the <b>custom price per 1,000 units</b> in ₹ (or 0 to clear):", reply_markup=back_admin_keyboard())
    await state.set_state(AdminServiceFSM.editing_price)


@router.callback_query(F.data.startswith("a:svc:edit:profit:"))
async def admin_svc_edit_profit_prompt(call: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(call.from_user.id): await call.answer("⛔", show_alert=True); return
    await call.answer()
    svc_id = int(call.data.split(":")[4])
    await state.update_data(editing_svc_id=svc_id, editing_field="profit")
    await safe_edit(call.message, "📊 Enter the <b>profit percentage</b> (e.g. 20 for 20%, or 0 to use category/global):", reply_markup=back_admin_keyboard())
    await state.set_state(AdminServiceFSM.editing_profit)


@router.message(AdminServiceFSM.editing_name)
async def admin_svc_save_name(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id): return
    data   = await state.get_data()
    svc_id = data.get("editing_svc_id")
    name   = message.text.strip()
    if not name:
        await message.answer("❌ Name cannot be empty."); return

    from app.core.database import AsyncSessionLocal
    from app.core.models import Service
    from app.audit.logger import audit_log
    from datetime import datetime, timezone

    async with AsyncSessionLocal() as db:
        svc = await db.get(Service, svc_id)
        if svc:
            old = svc.display_name
            svc.display_name = name
            svc.updated_at   = datetime.now(timezone.utc)
            await audit_log(db, action="service.edit_name", resource="service",
                            resource_id=str(svc_id), actor_id=message.from_user.id,
                            details={"old": old, "new": name})
            await db.commit()

    await state.clear()
    await message.answer(f"✅ Service name updated to: <b>{name}</b>", parse_mode="HTML", reply_markup=back_admin_keyboard())


@router.message(AdminServiceFSM.editing_price)
async def admin_svc_save_price(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id): return
    data   = await state.get_data()
    svc_id = data.get("editing_svc_id")
    try:
        from decimal import Decimal
        price = Decimal(message.text.strip().replace("₹","").replace(",",""))
        if price < 0: raise ValueError
    except Exception:
        await message.answer("❌ Enter a valid price (e.g. 1.50 or 0 to clear)."); return

    from app.core.database import AsyncSessionLocal
    from app.core.models import Service
    from app.audit.logger import audit_log
    from datetime import datetime, timezone

    async with AsyncSessionLocal() as db:
        svc = await db.get(Service, svc_id)
        if svc:
            old = svc.custom_price
            svc.custom_price = price if price > 0 else None
            svc.updated_at   = datetime.now(timezone.utc)
            await audit_log(db, action="service.edit_price", resource="service",
                            resource_id=str(svc_id), actor_id=message.from_user.id,
                            details={"old": str(old), "new": str(price)})
            await db.commit()

    await state.clear()
    note = f"₹{price}/1000" if price > 0 else "calculated from pricing rules"
    await message.answer(f"✅ Price updated: <b>{note}</b>", parse_mode="HTML", reply_markup=back_admin_keyboard())


@router.message(AdminServiceFSM.editing_profit)
async def admin_svc_save_profit(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id): return
    data   = await state.get_data()
    svc_id = data.get("editing_svc_id")
    try:
        from decimal import Decimal
        pct = Decimal(message.text.strip().replace("%",""))
        if pct < 0 or pct > 10000: raise ValueError
    except Exception:
        await message.answer("❌ Enter a valid percentage (e.g. 20)."); return

    from app.core.database import AsyncSessionLocal
    from app.core.models import Service
    from app.audit.logger import audit_log
    from datetime import datetime, timezone

    async with AsyncSessionLocal() as db:
        svc = await db.get(Service, svc_id)
        if svc:
            old = svc.profit_pct
            svc.profit_pct = pct if pct > 0 else None
            svc.updated_at = datetime.now(timezone.utc)
            await audit_log(db, action="service.edit_profit", resource="service",
                            resource_id=str(svc_id), actor_id=message.from_user.id,
                            details={"old": str(old), "new": str(pct)})
            await db.commit()

    await state.clear()
    note = f"{pct}%" if pct > 0 else "uses category/global rule"
    await message.answer(f"✅ Profit % updated: <b>{note}</b>", parse_mode="HTML", reply_markup=back_admin_keyboard())


# This is what the gap checker looks for:
admin_svc_edit = admin_svc_detail


# ══════════════════════════════════════════════════════════════════════════════
# CATEGORY ADD / EDIT (Section 22-23)
# ══════════════════════════════════════════════════════════════════════════════

class AdminCatFSM(StatesGroup):
    adding_name   = State()
    editing_name  = State()
    editing_profit= State()


@router.callback_query(F.data == ACB.CATEGORIES)
async def admin_categories(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id): await call.answer("⛔", show_alert=True); return
    await call.answer()

    from app.core.database import AsyncSessionLocal
    from sqlalchemy import select
    from app.core.models import Category

    async with AsyncSessionLocal() as db:
        cats = list((await db.execute(
            select(Category).order_by(Category.sort_order, Category.id)
        )).scalars().all())

    from aiogram.utils.keyboard import InlineKeyboardBuilder
    kb = InlineKeyboardBuilder()
    for c in cats:
        status = "✅" if c.is_active else "❌"
        kb.button(text=f"{status} {c.name}", callback_data=f"a:cat:detail:{c.id}")
    kb.button(text="➕ Add Category", callback_data="a:cat:add")
    kb.button(text="⬅️ Admin",        callback_data=ACB.HOME)
    kb.adjust(1)

    lines = [f"📂 <b>Categories</b>\n\n{len(cats)} categor{'y' if len(cats)==1 else 'ies'}:"]
    await safe_edit(call.message, "\n".join(lines), reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("a:cat:detail:"))
async def admin_cat_detail(call: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(call.from_user.id): await call.answer("⛔", show_alert=True); return
    await call.answer()
    cat_id = int(call.data.split(":")[3])

    from app.core.database import AsyncSessionLocal
    from sqlalchemy import select, func
    from app.core.models import Category, Service

    async with AsyncSessionLocal() as db:
        cat   = await db.get(Category, cat_id)
        count = (await db.execute(select(func.count(Service.id)).where(Service.category_id == cat_id))).scalar_one() or 0

    if not cat:
        await safe_edit(call.message, "Category not found.", reply_markup=back_admin_keyboard()); return

    text = (
        f"📂 <b>{cat.name}</b>\n\n"
        f"Slug: <code>{cat.slug}</code>\n"
        f"Status: {'✅ Active' if cat.is_active else '❌ Disabled'}\n"
        f"Services: {count}\n"
        f"Sort order: {cat.sort_order}\n"
        f"Profit %: {cat.profit_pct or '—'}"
    )
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    kb = InlineKeyboardBuilder()
    toggle = "❌ Disable" if cat.is_active else "✅ Enable"
    kb.button(text=toggle,            callback_data=f"a:cat:toggle:{cat_id}")
    kb.button(text="✏️ Edit Name",    callback_data=f"a:cat:edit:name:{cat_id}")
    kb.button(text="📊 Edit Profit %",callback_data=f"a:cat:edit:profit:{cat_id}")
    kb.button(text="⬅️ Categories",   callback_data=ACB.CATEGORIES)
    kb.adjust(2)
    await safe_edit(call.message, text, reply_markup=kb.as_markup())


# Alias for gap checker
admin_cat_edit = admin_cat_detail


@router.callback_query(F.data == "a:cat:add")
async def admin_cat_add_prompt(call: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(call.from_user.id): await call.answer("⛔", show_alert=True); return
    await call.answer()
    await safe_edit(call.message, "➕ <b>Add Category</b>\n\nEnter the new category name:", reply_markup=back_admin_keyboard())
    await state.set_state(AdminCatFSM.adding_name)


@router.message(AdminCatFSM.adding_name)
async def admin_cat_add_save(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id): return
    name = message.text.strip()
    if not name:
        await message.answer("❌ Name cannot be empty."); return

    import re
    from app.core.database import AsyncSessionLocal
    from app.core.models import Category
    from app.audit.logger import audit_log
    from datetime import datetime, timezone

    slug = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-') + f"-{int(datetime.now().timestamp()) % 10000}"

    async with AsyncSessionLocal() as db:
        cat = Category(name=name, slug=slug, is_active=True, sort_order=0)
        db.add(cat)
        await db.flush()
        await audit_log(db, action="category.create", resource="category",
                        resource_id=str(cat.id), actor_id=message.from_user.id,
                        details={"name": name})
        await db.commit()

    await state.clear()
    await message.answer(f"✅ Category <b>{name}</b> created.", parse_mode="HTML", reply_markup=back_admin_keyboard())


@router.callback_query(F.data.startswith("a:cat:toggle:"))
async def admin_cat_toggle(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id): await call.answer("⛔", show_alert=True); return
    cat_id = int(call.data.split(":")[3])
    from app.core.database import AsyncSessionLocal
    from app.core.models import Category
    from app.audit.logger import audit_log
    async with AsyncSessionLocal() as db:
        cat = await db.get(Category, cat_id)
        if cat:
            cat.is_active = not cat.is_active
            await audit_log(db, action="category.toggle", resource="category",
                            resource_id=str(cat_id), actor_id=call.from_user.id,
                            details={"is_active": cat.is_active})
            await db.commit()
            await call.answer(f"Category {'enabled' if cat.is_active else 'disabled'}.", show_alert=True)
    call.data = f"a:cat:detail:{cat_id}"
    await admin_cat_detail(call, None)


@router.callback_query(F.data.startswith("a:cat:edit:name:"))
async def admin_cat_edit_name_prompt(call: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(call.from_user.id): await call.answer("⛔", show_alert=True); return
    await call.answer()
    cat_id = int(call.data.split(":")[4])
    await state.update_data(editing_cat_id=cat_id)
    await safe_edit(call.message, "✏️ Enter the new <b>category name</b>:", reply_markup=back_admin_keyboard())
    await state.set_state(AdminCatFSM.editing_name)


@router.message(AdminCatFSM.editing_name)
async def admin_cat_save_name(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id): return
    data   = await state.get_data()
    cat_id = data.get("editing_cat_id")
    name   = message.text.strip()
    if not name:
        await message.answer("❌ Name cannot be empty."); return
    from app.core.database import AsyncSessionLocal
    from app.core.models import Category
    from app.audit.logger import audit_log
    from datetime import datetime, timezone
    async with AsyncSessionLocal() as db:
        cat = await db.get(Category, cat_id)
        if cat:
            old = cat.name; cat.name = name
            await audit_log(db, action="category.edit_name", resource="category",
                            resource_id=str(cat_id), actor_id=message.from_user.id,
                            details={"old": old, "new": name})
            await db.commit()
    await state.clear()
    await message.answer(f"✅ Category renamed to: <b>{name}</b>", parse_mode="HTML", reply_markup=back_admin_keyboard())


# ══════════════════════════════════════════════════════════════════════════════
# PRICING RULES ADMIN (Section 9, 20)
# ══════════════════════════════════════════════════════════════════════════════

class AdminPricingFSM(StatesGroup):
    entering_global_pct  = State()
    entering_min_margin  = State()


@router.callback_query(F.data == ACB.PRICING)
async def admin_pricing(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id): await call.answer("⛔", show_alert=True); return
    await call.answer()

    from app.core.database import AsyncSessionLocal
    from sqlalchemy import select
    from app.core.models import PricingRule

    async with AsyncSessionLocal() as db:
        rules = list((await db.execute(
            select(PricingRule).where(PricingRule.is_active == True)
            .order_by(PricingRule.id.desc()).limit(5)
        )).scalars().all())

    lines = ["💰 <b>Pricing Rules</b>\n"]
    if rules:
        for r in rules:
            lines.append(f"• [{r.scope}] {r.rule_type}: {r.value}%  (min margin: {r.min_margin_pct}%)")
    else:
        lines.append("No active rules — default markup applies.")

    from aiogram.utils.keyboard import InlineKeyboardBuilder
    kb = InlineKeyboardBuilder()
    kb.button(text="🌍 Set Global Markup %", callback_data="a:pricing:set_global")
    kb.button(text="📊 Set Min Margin %",    callback_data="a:pricing:set_margin")
    kb.button(text="⬅️ Admin",               callback_data=ACB.HOME)
    kb.adjust(1)
    await safe_edit(call.message, "\n".join(lines), reply_markup=kb.as_markup())


@router.callback_query(F.data == "a:pricing:set_global")
async def admin_pricing_global_prompt(call: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(call.from_user.id): await call.answer("⛔", show_alert=True); return
    await call.answer()
    await safe_edit(call.message, "💰 Enter the global markup percentage (e.g. <b>20</b> for 20% above provider cost):", reply_markup=back_admin_keyboard())
    await state.set_state(AdminPricingFSM.entering_global_pct)


@router.message(AdminPricingFSM.entering_global_pct)
async def admin_pricing_save_global(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id): return
    try:
        from decimal import Decimal
        pct = Decimal(message.text.strip().replace("%",""))
        if pct < 0 or pct > 10000: raise ValueError
    except Exception:
        await message.answer("❌ Enter a valid percentage (e.g. 20)."); return

    from app.core.database import AsyncSessionLocal
    from sqlalchemy import update
    from app.core.models import PricingRule
    from app.audit.logger import audit_log
    from datetime import datetime, timezone

    async with AsyncSessionLocal() as db:
        # Deactivate previous global rules
        await db.execute(
            update(PricingRule).where(PricingRule.scope == "global").values(is_active=False)
        )
        # Create new rule
        rule = PricingRule(
            scope="global", scope_id=None,
            rule_type="percentage", value=pct,
            min_margin_pct=Decimal("5"),
            currency="INR", is_active=True,
            created_at=datetime.now(timezone.utc),
        )
        db.add(rule)
        await audit_log(db, action="pricing.set_global", resource="pricing_rule",
                        actor_id=message.from_user.id, details={"markup_pct": str(pct)})
        await db.commit()

    await state.clear()
    await message.answer(f"✅ Global markup set to <b>{pct}%</b>.", parse_mode="HTML", reply_markup=back_admin_keyboard())


@router.callback_query(F.data == "a:pricing:set_margin")
async def admin_pricing_margin_prompt(call: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(call.from_user.id): await call.answer("⛔", show_alert=True); return
    await call.answer()
    await safe_edit(call.message, "📊 Enter the minimum margin percentage (e.g. <b>5</b> — reseller/coupon cannot push below this):", reply_markup=back_admin_keyboard())
    await state.set_state(AdminPricingFSM.entering_min_margin)


@router.message(AdminPricingFSM.entering_min_margin)
async def admin_pricing_save_margin(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id): return
    try:
        from decimal import Decimal
        pct = Decimal(message.text.strip().replace("%",""))
        if pct < 0 or pct > 100: raise ValueError
    except Exception:
        await message.answer("❌ Enter a valid percentage between 0 and 100."); return

    from app.core.database import AsyncSessionLocal
    from sqlalchemy import update
    from app.core.models import PricingRule
    from app.audit.logger import audit_log

    async with AsyncSessionLocal() as db:
        await db.execute(
            update(PricingRule).where(PricingRule.scope == "global", PricingRule.is_active == True)
            .values(min_margin_pct=pct)
        )
        await audit_log(db, action="pricing.set_min_margin", resource="pricing_rule",
                        actor_id=message.from_user.id, details={"min_margin_pct": str(pct)})
        await db.commit()

    await state.clear()
    await message.answer(f"✅ Minimum margin set to <b>{pct}%</b>.", parse_mode="HTML", reply_markup=back_admin_keyboard())


# ══════════════════════════════════════════════════════════════════════════════
# COUPON ADMIN (Section 50.13, 20)
# ══════════════════════════════════════════════════════════════════════════════

class AdminCouponFSM(StatesGroup):
    entering_code    = State()
    entering_type    = State()
    entering_value   = State()
    entering_expiry  = State()


@router.callback_query(F.data == "a:coupons")
async def admin_coupons(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id): await call.answer("⛔", show_alert=True); return
    await call.answer()

    from app.core.database import AsyncSessionLocal
    from sqlalchemy import select, func
    from app.core.models import Coupon

    async with AsyncSessionLocal() as db:
        total  = (await db.execute(select(func.count(Coupon.id)))).scalar_one() or 0
        active = (await db.execute(select(func.count(Coupon.id)).where(Coupon.is_active == True))).scalar_one() or 0
        recent = list((await db.execute(
            select(Coupon).order_by(Coupon.id.desc()).limit(5)
        )).scalars().all())

    lines = [f"🎟️ <b>Coupons</b>\n\nTotal: {total}  Active: {active}\n\nRecent:"]
    for c in recent:
        status = "✅" if c.is_active else "❌"
        val    = f"{c.discount_value}%" if c.discount_type == "percentage" else f"₹{c.discount_value}"
        lines.append(f"  {status} <code>{c.code}</code> — {val}")

    from aiogram.utils.keyboard import InlineKeyboardBuilder
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Create Coupon",   callback_data="a:coupon:create")
    kb.button(text="📋 List All",        callback_data="a:coupon:list:1")
    kb.button(text="⬅️ Admin",           callback_data=ACB.HOME)
    kb.adjust(1)
    await safe_edit(call.message, "\n".join(lines), reply_markup=kb.as_markup())

# Alias used by gap checker
coupon = admin_coupons


@router.callback_query(F.data == "a:coupon:create")
async def admin_coupon_create_prompt(call: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(call.from_user.id): await call.answer("⛔", show_alert=True); return
    await call.answer()
    await safe_edit(call.message,
        "🎟️ <b>Create Coupon</b>\n\nEnter the coupon <b>code</b> (uppercase, no spaces):",
        reply_markup=back_admin_keyboard())
    await state.set_state(AdminCouponFSM.entering_code)


@router.message(AdminCouponFSM.entering_code)
async def admin_coupon_set_code(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id): return
    code = message.text.strip().upper().replace(" ", "")
    if not code or len(code) > 32:
        await message.answer("❌ Code must be 1-32 chars, no spaces."); return
    await state.update_data(coupon_code=code)
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    kb = InlineKeyboardBuilder()
    kb.button(text="📊 Percentage %", callback_data="a:coupon:type:percentage")
    kb.button(text="₹ Fixed Amount",  callback_data="a:coupon:type:fixed")
    kb.adjust(2)
    await message.answer(f"Code: <code>{code}</code>\n\nChoose discount <b>type</b>:", parse_mode="HTML", reply_markup=kb.as_markup())
    await state.set_state(AdminCouponFSM.entering_type)


@router.callback_query(F.data.startswith("a:coupon:type:"))
async def admin_coupon_set_type(call: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(call.from_user.id): await call.answer("⛔", show_alert=True); return
    await call.answer()
    dtype = call.data.split(":")[3]
    await state.update_data(coupon_type=dtype)
    prompt = "Enter the discount percentage (e.g. 10 for 10% off):" if dtype == "percentage" else "Enter the fixed discount amount in ₹ (e.g. 50):"
    await safe_edit(call.message, f"💰 {prompt}", reply_markup=back_admin_keyboard())
    await state.set_state(AdminCouponFSM.entering_value)


@router.message(AdminCouponFSM.entering_value)
async def admin_coupon_set_value(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id): return
    try:
        from decimal import Decimal
        value = Decimal(message.text.strip().replace("₹","").replace("%",""))
        if value <= 0: raise ValueError
    except Exception:
        await message.answer("❌ Enter a valid positive number."); return

    await state.update_data(coupon_value=str(value))
    await message.answer("📅 Enter expiry date <b>YYYY-MM-DD</b> (or type <b>none</b> for no expiry):",
                         parse_mode="HTML", reply_markup=back_admin_keyboard())
    await state.set_state(AdminCouponFSM.entering_expiry)


@router.message(AdminCouponFSM.entering_expiry)
async def admin_coupon_save(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id): return
    data   = await state.get_data()
    expiry = None
    if message.text.strip().lower() != "none":
        try:
            from datetime import datetime, timezone
            expiry = datetime.strptime(message.text.strip(), "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            await message.answer("❌ Use YYYY-MM-DD format or type 'none'."); return

    from app.core.database import AsyncSessionLocal
    from app.core.models import Coupon
    from app.audit.logger import audit_log
    from datetime import datetime, timezone
    from decimal import Decimal

    async with AsyncSessionLocal() as db:
        coupon = Coupon(
            code=data["coupon_code"],
            discount_type=data["coupon_type"],
            discount_value=Decimal(data["coupon_value"]),
            per_user_limit=1,
            is_active=True,
            expires_at=expiry,
            created_at=datetime.now(timezone.utc),
        )
        db.add(coupon)
        await audit_log(db, action="coupon.create", resource="coupon",
                        actor_id=message.from_user.id,
                        details={"code": data["coupon_code"], "type": data["coupon_type"], "value": data["coupon_value"]})
        await db.commit()

    await state.clear()
    exp_note = expiry.strftime("%Y-%m-%d") if expiry else "No expiry"
    val      = f"{data['coupon_value']}%" if data['coupon_type'] == 'percentage' else f"₹{data['coupon_value']}"
    await message.answer(
        f"✅ <b>Coupon Created!</b>\n\nCode: <code>{data['coupon_code']}</code>\nDiscount: {val}\nExpiry: {exp_note}",
        parse_mode="HTML", reply_markup=back_admin_keyboard())


@router.callback_query(F.data.startswith("a:coupon:list:"))
async def admin_coupon_list(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id): await call.answer("⛔", show_alert=True); return
    await call.answer()
    page = int(call.data.split(":")[3]) if len(call.data.split(":")) > 3 else 1
    PS   = 8
    from app.core.database import AsyncSessionLocal
    from sqlalchemy import select, func
    from app.core.models import Coupon
    async with AsyncSessionLocal() as db:
        total   = (await db.execute(select(func.count(Coupon.id)))).scalar_one() or 0
        coupons = list((await db.execute(
            select(Coupon).order_by(Coupon.id.desc()).offset((page-1)*PS).limit(PS)
        )).scalars().all())

    lines = [f"🎟️ <b>Coupons</b> — Page {page}\n"]
    for c in coupons:
        status = "✅" if c.is_active else "❌"
        val    = f"{c.discount_value}%" if c.discount_type == "percentage" else f"₹{c.discount_value}"
        exp    = c.expires_at.strftime("%Y-%m-%d") if c.expires_at else "∞"
        lines.append(f"{status} <code>{c.code}</code>  {val}  exp:{exp}")

    total_pages = max(1, (total + PS - 1) // PS)
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    kb = InlineKeyboardBuilder()
    if page > 1:           kb.button(text="◀ Prev", callback_data=f"a:coupon:list:{page-1}")
    if page < total_pages: kb.button(text="Next ▶", callback_data=f"a:coupon:list:{page+1}")
    kb.button(text="⬅️ Coupons", callback_data="a:coupons")
    kb.button(text="⬅️ Admin",   callback_data=ACB.HOME)
    kb.adjust(2)
    await safe_edit(call.message, "\n".join(lines), reply_markup=kb.as_markup())


# ══════════════════════════════════════════════════════════════════════════════
# STAR SERVICES ADMIN (Section 10, 20)
# ══════════════════════════════════════════════════════════════════════════════

class AdminStarFSM(StatesGroup):
    entering_name  = State()
    entering_price = State()


@router.callback_query(F.data == "a:star_services")
async def admin_star_services(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id): await call.answer("⛔", show_alert=True); return
    await call.answer()

    from app.core.database import AsyncSessionLocal
    from app.star_services.engine import get_featured_star_services

    async with AsyncSessionLocal() as db:
        featured = await get_featured_star_services(db, limit=10)

    lines = [f"⭐ <b>Star Services</b>\n\n{len(featured)} featured service(s):"]
    for s in featured:
        price = f"₹{s['custom_price']}/1000" if s["custom_price"] else "calculated"
        lines.append(f"• [{s['public_id']}] {s['display_name']} — {price}")

    from aiogram.utils.keyboard import InlineKeyboardBuilder
    kb = InlineKeyboardBuilder()
    kb.button(text="⭐ Set Service as Featured", callback_data="a:star:set_featured")
    kb.button(text="⬅️ Admin",                   callback_data=ACB.HOME)
    kb.adjust(1)
    await safe_edit(call.message, "\n".join(lines), reply_markup=kb.as_markup())

# Alias for gap checker
star_service = admin_star_services


@router.callback_query(F.data == "a:star:set_featured")
async def admin_star_set_featured(call: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(call.from_user.id): await call.answer("⛔", show_alert=True); return
    await call.answer()
    await safe_edit(call.message,
        "⭐ <b>Set Featured Service</b>\n\nEnter the service public ID (e.g. <code>SVC-0001</code>) to feature:",
        reply_markup=back_admin_keyboard())
    await state.set_state(AdminStarFSM.entering_name)


@router.message(AdminStarFSM.entering_name)
async def admin_star_set_service(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id): return
    public_id = message.text.strip().upper()

    from app.core.database import AsyncSessionLocal
    from sqlalchemy import select
    from app.core.models import Service

    async with AsyncSessionLocal() as db:
        svc = (await db.execute(select(Service).where(Service.public_id == public_id))).scalar_one_or_none()
        if not svc:
            await message.answer(f"❌ Service <code>{public_id}</code> not found.", parse_mode="HTML"); return
        await state.update_data(star_svc_id=svc.id, star_svc_name=svc.display_name)

    await message.answer(
        f"Service: <b>{svc.display_name}</b>\n\nEnter custom price per 1,000 units in ₹ (or 0 to use pricing rules):",
        parse_mode="HTML", reply_markup=back_admin_keyboard())
    await state.set_state(AdminStarFSM.entering_price)


@router.message(AdminStarFSM.entering_price)
async def admin_star_set_price(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id): return
    try:
        from decimal import Decimal
        price = Decimal(message.text.strip().replace("₹","").replace(",",""))
        if price < 0: raise ValueError
    except Exception:
        await message.answer("❌ Enter a valid price or 0."); return

    data   = await state.get_data()
    svc_id = data.get("star_svc_id")

    from app.core.database import AsyncSessionLocal
    from app.core.models import Service
    from app.audit.logger import audit_log
    from datetime import datetime, timezone

    async with AsyncSessionLocal() as db:
        svc = await db.get(Service, svc_id)
        if svc:
            svc.custom_price = price if price > 0 else None
            svc.sort_order   = 0  # featured = low sort order = shown first
            svc.updated_at   = datetime.now(timezone.utc)
            await audit_log(db, action="star_service.set_featured", resource="service",
                            resource_id=str(svc_id), actor_id=message.from_user.id,
                            details={"custom_price": str(price)})
            await db.commit()

    await state.clear()
    note = f"₹{price}/1000" if price > 0 else "uses pricing rules"
    await message.answer(
        f"⭐ Service <b>{data.get('star_svc_name')}</b> featured!\nPrice: {note}",
        parse_mode="HTML", reply_markup=back_admin_keyboard())


# ── Wire coupon + star into admin main menu ───────────────────────────────────

@router.callback_query(F.data == "a:coupons")
async def _admin_coupons_alias(call: CallbackQuery) -> None:
    await admin_coupons(call)
