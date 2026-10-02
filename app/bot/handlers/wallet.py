"""Balance, ledger, deposits (Razorpay link / manual / demo)."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.ads import apply_ad
from app.bot.handlers.start import ensure_user
from app.bot.menus import (
    MenuCB,
    WalCB,
    back_home_kb,
    deposit_kb,
    html_escape,
    manual_invoice_kb,
    mock_invoice_kb,
    pay_link_kb,
    safe_edit,
)
from app.catalog import assert_payments_open, get_setting
from app.config import Settings
from app.models import Payment
from app.payments.service import DepositError, check_payment, create_deposit, settle_payment
from app.pricing import paise_to_rupees_str
from app.wallet import list_entries, snapshot_of

router = Router(name="wallet")


def _balance_text(user) -> str:
    snap = snapshot_of(user)
    return (
        "<b>Wallet</b>\n\n"
        f"Available: <b>{paise_to_rupees_str(snap.available_paise)}</b>\n"
        f"Reserved: {paise_to_rupees_str(snap.reserved_paise)}\n"
        f"Ledger balance: {paise_to_rupees_str(snap.balance_paise)}\n\n"
        "Reserved funds are held while an order is being submitted."
    )


@router.callback_query(MenuCB.filter(F.a == "balance"))
async def cb_balance(
    query: CallbackQuery, session: AsyncSession, settings: Settings
) -> None:
    if query.from_user is None:
        return
    user = await ensure_user(session, query.from_user, settings)
    text, markup = await apply_ad(session, "wallet", _balance_text(user), back_home_kb())
    await safe_edit(query, text, markup)


@router.message(Command("balance"))
async def cmd_balance(message: Message, session: AsyncSession, settings: Settings) -> None:
    if message.from_user is None:
        return
    user = await ensure_user(session, message.from_user, settings)
    text, markup = await apply_ad(session, "wallet", _balance_text(user), back_home_kb())
    await message.answer(text, parse_mode="HTML", reply_markup=markup)


@router.callback_query(MenuCB.filter(F.a == "ledger"))
async def cb_ledger(
    query: CallbackQuery, session: AsyncSession, settings: Settings
) -> None:
    if query.from_user is None:
        return
    user = await ensure_user(session, query.from_user, settings)
    entries = await list_entries(session, user.telegram_id, limit=12)
    if not entries:
        await safe_edit(query, "<b>Ledger</b>\nNo movements yet.", back_home_kb())
        return
    lines = ["<b>Recent ledger</b>"]
    for entry in entries:
        sign = "+" if entry.amount_paise > 0 else ""
        if entry.amount_paise:
            amount = f"{sign}{paise_to_rupees_str(entry.amount_paise)}"
        else:
            amount = entry.entry_type
        lines.append(
            f"• {html_escape(entry.entry_type)} {html_escape(str(amount))} — {html_escape(entry.reason)}"
        )
    await safe_edit(query, "\n".join(lines), back_home_kb())


@router.callback_query(MenuCB.filter(F.a == "deposit"))
async def cb_deposit(query: CallbackQuery, session: AsyncSession) -> None:
    try:
        await assert_payments_open(session)
    except PermissionError as exc:
        await safe_edit(query, html_escape(str(exc)), back_home_kb())
        return
    text, markup = await apply_ad(
        session,
        "wallet",
        "<b>Deposit</b>\nChoose an amount to add to your wallet.",
        deposit_kb(),
    )
    await safe_edit(query, text, markup)


@router.callback_query(WalCB.filter(F.a == "amt"))
async def cb_deposit_amount(
    query: CallbackQuery, callback_data: WalCB, session: AsyncSession, settings: Settings
) -> None:
    if query.from_user is None:
        return
    try:
        amount = int(callback_data.v)
    except ValueError:
        await query.answer("Invalid amount", show_alert=True)
        return
    user = await ensure_user(session, query.from_user, settings)
    try:
        payment = await create_deposit(session, user, amount, settings=settings)
    except DepositError as exc:
        await query.answer(str(exc)[:190], show_alert=True)
        return

    head = (
        f"<b>Invoice {html_escape(payment.public_id)}</b>\n"
        f"Amount: <b>{paise_to_rupees_str(payment.amount_paise)}</b>\n\n"
    )
    if payment.method == "razorpay" and payment.checkout_url:
        await safe_edit(
            query,
            head + "Tap <b>Pay securely</b>. Your wallet is credited automatically once the payment is "
            "confirmed by the payment provider (usually within a minute).",
            pay_link_kb(payment.checkout_url, payment.public_id),
        )
    elif payment.method == "manual":
        instructions = await get_setting(session, "payment_instructions", "")
        body = html_escape(instructions) if instructions else "Contact support to complete this payment."
        await safe_edit(
            query,
            head + f"{body}\n\nQuote the invoice number above. An admin credits your wallet after "
            "verifying the payment.",
            manual_invoice_kb(payment.public_id),
        )
    elif settings.mock_payments_allowed:
        await safe_edit(
            query,
            head + "Demo mode: tap Pay now to credit this invoice. Not available in production.",
            mock_invoice_kb(payment.public_id),
        )
    else:
        await query.answer("Deposits are not available right now.", show_alert=True)


@router.callback_query(WalCB.filter(F.a == "chk"))
async def cb_check_payment(
    query: CallbackQuery, callback_data: WalCB, session: AsyncSession, settings: Settings
) -> None:
    """User asks 'did my payment arrive?'. We ask the gateway; their claim proves nothing."""
    if query.from_user is None:
        return
    user = await ensure_user(session, query.from_user, settings)
    payment = await check_payment(session, callback_data.v, user.telegram_id, settings=settings)
    if payment is None:
        await query.answer("Invoice not found", show_alert=True)
        return
    await session.refresh(user)
    if payment.status == "paid":
        await safe_edit(query, "Payment confirmed.\n\n" + _balance_text(user), back_home_kb())
    elif payment.status == "review":
        await safe_edit(
            query,
            "This payment needs a manual check by our team. It will be resolved shortly.",
            back_home_kb(),
        )
    elif payment.status == "expired":
        await safe_edit(query, "This invoice expired. Start a new deposit.", back_home_kb())
    else:
        await query.answer("Not confirmed yet. If you have paid, wait a minute and check again.", show_alert=True)


@router.callback_query(WalCB.filter(F.a == "pay"))
async def cb_mock_pay(
    query: CallbackQuery, callback_data: WalCB, session: AsyncSession, settings: Settings
) -> None:
    if query.from_user is None:
        return
    # Hard gate: this handler can never credit money in production.
    if not settings.mock_payments_allowed:
        await query.answer("Demo payments are disabled.", show_alert=True)
        return
    try:
        await assert_payments_open(session)
    except PermissionError as exc:
        await query.answer(str(exc), show_alert=True)
        return
    from sqlalchemy import select

    payment = (
        await session.execute(select(Payment).where(Payment.public_id == callback_data.v))
    ).scalar_one_or_none()
    if payment is None or payment.user_id != query.from_user.id or payment.method != "mock":
        await query.answer("Invoice not found", show_alert=True)
        return
    user = await ensure_user(session, query.from_user, settings)
    await settle_payment(
        session,
        payment_public_id=payment.public_id,
        gateway="mock",
        gateway_ref=None,
        amount_paise=payment.amount_paise,
        currency=payment.currency,
        source="mock",
        actor=str(user.telegram_id),
    )
    await session.refresh(user)
    text, markup = await apply_ad(
        session,
        "wallet",
        f"Payment received. Credited {paise_to_rupees_str(payment.amount_paise)}.\n\n" + _balance_text(user),
        back_home_kb(),
    )
    await safe_edit(query, text, markup)
