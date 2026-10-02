"""Personal stats plus staff platform snapshot."""

from __future__ import annotations

from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.handlers.start import ensure_user, is_staff
from app.bot.menus import MenuCB, back_home_kb, html_escape, safe_edit
from app.config import Settings
from app.models import ACTIVE_STATUSES, LedgerEntry, Order, Payment, Provider, User
from app.pricing import paise_to_rupees_str
from app.wallet import snapshot_of

router = Router(name="stats")


def _day_start() -> datetime:
    return datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


async def _personal_stats(session: AsyncSession, user: User) -> str:
    uid = user.telegram_id
    total = int(
        (await session.execute(select(func.count()).select_from(Order).where(Order.user_id == uid))).scalar_one()
    )
    completed = int(
        (
            await session.execute(
                select(func.count()).select_from(Order).where(Order.user_id == uid, Order.status == "completed")
            )
        ).scalar_one()
    )
    processing = int(
        (
            await session.execute(
                select(func.count())
                .select_from(Order)
                .where(Order.user_id == uid, Order.status.in_(tuple(ACTIVE_STATUSES)))
            )
        ).scalar_one()
    )
    failed = int(
        (
            await session.execute(
                select(func.count()).select_from(Order).where(Order.user_id == uid, Order.status == "failed")
            )
        ).scalar_one()
    )
    spent = int(
        (
            await session.execute(
                select(func.coalesce(func.sum(Order.charge_paise), 0)).where(
                    Order.user_id == uid,
                    Order.status.notin_(("failed", "canceled", "cancelled")),
                )
            )
        ).scalar_one()
    )
    deposits = int(
        (
            await session.execute(
                select(func.coalesce(func.sum(Payment.amount_paise), 0)).where(
                    Payment.user_id == uid, Payment.status == "paid"
                )
            )
        ).scalar_one()
    )
    refunds = int(
        (
            await session.execute(
                select(func.coalesce(func.sum(LedgerEntry.amount_paise), 0)).where(
                    LedgerEntry.user_id == uid,
                    LedgerEntry.entry_type == "credit",
                    or_(LedgerEntry.reason.like("Refund%"), LedgerEntry.reason.like("Partial%")),
                )
            )
        ).scalar_one()
    )
    refills = int(
        (
            await session.execute(
                select(func.coalesce(func.sum(Order.refill_count), 0)).where(Order.user_id == uid)
            )
        ).scalar_one()
    )
    referrals = int(
        (
            await session.execute(
                select(func.count()).select_from(User).where(User.referred_by == uid)
            )
        ).scalar_one()
    )
    snap = snapshot_of(user)
    return (
        "<b>Your stats</b>\n\n"
        f"Orders: {total}\n"
        f"Completed: {completed}\n"
        f"Processing: {processing}\n"
        f"Failed: {failed}\n"
        f"Spent: {paise_to_rupees_str(spent)}\n"
        f"Deposits: {paise_to_rupees_str(deposits)}\n"
        f"Refunds: {paise_to_rupees_str(refunds)}\n"
        f"Refills: {refills}\n"
        f"Referrals: {referrals}\n"
        f"Available balance: <b>{paise_to_rupees_str(snap.available_paise)}</b>"
    )


async def _platform_stats(session: AsyncSession) -> str:
    start = _day_start()
    users = int((await session.execute(select(func.count()).select_from(User))).scalar_one())
    orders_today = int(
        (
            await session.execute(
                select(func.count()).select_from(Order).where(Order.created_at >= start)
            )
        ).scalar_one()
    )
    gmv_today = int(
        (
            await session.execute(
                select(func.coalesce(func.sum(Order.charge_paise), 0)).where(
                    Order.created_at >= start,
                    Order.status.notin_(("failed", "canceled", "cancelled")),
                )
            )
        ).scalar_one()
    )
    open_orders = int(
        (
            await session.execute(
                select(func.count()).select_from(Order).where(Order.status.in_(tuple(ACTIVE_STATUSES)))
            )
        ).scalar_one()
    )
    providers = list((await session.execute(select(Provider))).scalars().all())
    alerts = [
        f"• {html_escape(p.name)} [{p.id}] {html_escape(p.health_status)}"
        f"{' — ' + html_escape(p.last_health_detail) if p.last_health_detail else ''}"
        for p in providers
        if (p.health_status or "unknown") not in {"ok", "unknown"}
    ]
    alert_block = "\n".join(alerts) if alerts else "None"
    return (
        "\n\n<b>Platform</b>\n"
        f"Total users: {users}\n"
        f"Orders today: {orders_today}\n"
        f"GMV today: {paise_to_rupees_str(gmv_today)}\n"
        f"Open orders: {open_orders}\n"
        f"Provider alerts:\n{alert_block}"
    )


async def stats_text(session: AsyncSession, user: User, settings: Settings) -> str:
    body = await _personal_stats(session, user)
    if is_staff(user, settings):
        body += await _platform_stats(session)
    return body


@router.callback_query(MenuCB.filter(F.a == "stats"))
async def cb_stats(
    query: CallbackQuery, session: AsyncSession, settings: Settings
) -> None:
    if query.from_user is None:
        return
    user = await ensure_user(session, query.from_user, settings)
    await safe_edit(query, await stats_text(session, user, settings), back_home_kb())


@router.message(Command("stats"))
async def cmd_stats(message: Message, session: AsyncSession, settings: Settings) -> None:
    if message.from_user is None:
        return
    user = await ensure_user(session, message.from_user, settings)
    await message.answer(
        await stats_text(session, user, settings),
        parse_mode="HTML",
        reply_markup=back_home_kb(),
    )
