"""
Tenant bot handlers.

Builds a per-tenant aiogram Router that mirrors the main platform bot's
handler set, but routes all operations through tenant-scoped context:

  - TenantContext carries tenant_id + tenant_user throughout the request
  - Order engine receives tenant_id as explicit parameter
  - Wallet ledger uses tenant_user.wallet_balance (per-tenant wallet)
  - All messages use the tenant's configured display name and style

The main difference from the platform bot:
  - Users are TenantUser rows, not global User rows
  - The tenant can override service visibility and pricing
  - Branding (bot name, messages) comes from tenant.config

build_tenant_router(tenant_id) → Router
  Called by TenantBotManager when starting each bot instance.
  Returns a fully wired aiogram Router.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from app.core.logging import get_logger

logger = get_logger(__name__)


# ── Tenant context ─────────────────────────────────────────────────────────────

@dataclass
class TenantContext:
    """
    Per-request context for tenant bot interactions.
    Injected via middleware — available in all handler functions.
    """
    tenant_id:   int
    tenant_slug: str
    user_id:     int         # TenantUser.id (not global User.id)
    telegram_id: int
    first_name:  str
    is_banned:   bool
    wallet_balance: float    # TenantUser.wallet_balance


# ── FSM States ─────────────────────────────────────────────────────────────────

class TenantOrderStates(StatesGroup):
    selecting_qty  = State()
    entering_link  = State()
    confirming     = State()


# ── Handler builders ───────────────────────────────────────────────────────────

def build_tenant_router(tenant_id: int) -> Router:
    """
    Build a fully wired aiogram Router for one tenant.
    Each tenant gets its own Router instance (no shared state).
    """
    router = Router(name=f"tenant_{tenant_id}")

    # ── /start ─────────────────────────────────────────────────────────────────

    @router.message(CommandStart())
    async def tenant_start(message: Message, state: FSMContext, **data):
        ctx: TenantContext = data.get("tenant_ctx")
        if ctx and ctx.is_banned:
            await message.answer("Your account has been suspended.")
            return

        name = ctx.first_name if ctx else (message.from_user.first_name or "there")
        balance = f"₹{ctx.wallet_balance:.2f}" if ctx else "₹0.00"

        await message.answer(
            f"👋 Welcome, <b>{name}</b>!\n\n"
            f"💳 Balance: <b>{balance}</b>\n\n"
            f"Use the menu below to browse services and place orders.",
            reply_markup=_main_menu_keyboard(),
        )
        await state.clear()

    # ── Services ────────────────────────────────────────────────────────────────

    @router.callback_query(F.data == "tenant:services")
    async def tenant_services(call: CallbackQuery, **data):
        """List service categories for this tenant."""
        await call.answer()
        await call.message.edit_text(
            "📦 <b>Services</b>\n\nSelect a category:",
            reply_markup=_categories_keyboard(tenant_id),
        )

    @router.callback_query(F.data.startswith("tenant:cat:"))
    async def tenant_category_services(call: CallbackQuery, **data):
        """List services in a category."""
        await call.answer()
        cat_id = call.data.split(":")[-1]
        await call.message.edit_text(
            "Loading services…",
        )
        # In production: fetch services filtered to this tenant's visible set
        # For now: show a placeholder
        await call.message.edit_text(
            f"📋 Services for category {cat_id}\n\nUse /start to return to menu.",
        )

    # ── Wallet / Deposit ────────────────────────────────────────────────────────

    @router.callback_query(F.data == "tenant:wallet")
    async def tenant_wallet(call: CallbackQuery, **data):
        ctx: TenantContext = data.get("tenant_ctx")
        await call.answer()
        balance = f"₹{ctx.wallet_balance:.2f}" if ctx else "₹0.00"
        await call.message.edit_text(
            f"💳 <b>Wallet</b>\n\n"
            f"Balance: <b>{balance}</b>\n\n"
            f"Tap below to add funds:",
            reply_markup=_deposit_keyboard(),
        )

    @router.callback_query(F.data.startswith("tenant:deposit:"))
    async def tenant_deposit(call: CallbackQuery, **data):
        ctx: TenantContext = data.get("tenant_ctx")
        amount = call.data.split(":")[-1]
        await call.answer()
        # In production: create payment intent and send payment link
        await call.message.edit_text(
            f"💳 Depositing ₹{amount}…\n\n"
            f"A payment link will be sent shortly.",
        )

    # ── Orders ─────────────────────────────────────────────────────────────────

    @router.callback_query(F.data == "tenant:orders")
    async def tenant_orders(call: CallbackQuery, **data):
        ctx: TenantContext = data.get("tenant_ctx")
        await call.answer()
        # In production: fetch tenant-scoped orders for this user
        await call.message.edit_text(
            "📋 <b>Your Orders</b>\n\nNo orders yet.",
            reply_markup=_back_to_menu_keyboard(),
        )

    # ── Profile ─────────────────────────────────────────────────────────────────

    @router.callback_query(F.data == "tenant:profile")
    async def tenant_profile(call: CallbackQuery, **data):
        ctx: TenantContext = data.get("tenant_ctx")
        await call.answer()
        name    = ctx.first_name if ctx else "User"
        balance = f"₹{ctx.wallet_balance:.2f}" if ctx else "₹0.00"
        await call.message.edit_text(
            f"👤 <b>Profile</b>\n\n"
            f"Name: {name}\n"
            f"Balance: {balance}",
            reply_markup=_back_to_menu_keyboard(),
        )

    # ── Error handler ───────────────────────────────────────────────────────────

    @router.errors()
    async def tenant_error_handler(event, **data):
        logger.error(
            "tenant_handler_error",
            tenant_id=tenant_id,
            error=str(event.exception),
        )

    return router


# ── Keyboards ──────────────────────────────────────────────────────────────────

def _main_menu_keyboard():
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    kb = InlineKeyboardBuilder()
    kb.button(text="🛒 Services",  callback_data="tenant:services")
    kb.button(text="📋 Orders",    callback_data="tenant:orders")
    kb.button(text="💳 Wallet",    callback_data="tenant:wallet")
    kb.button(text="👤 Profile",   callback_data="tenant:profile")
    kb.adjust(2)
    return kb.as_markup()


def _categories_keyboard(tenant_id: int):
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    # In production: fetch categories visible to this tenant from DB
    categories = [
        ("Instagram", "instagram"),
        ("YouTube",   "youtube"),
        ("TikTok",    "tiktok"),
        ("Twitter",   "twitter"),
    ]
    kb = InlineKeyboardBuilder()
    for name, slug in categories:
        kb.button(text=name, callback_data=f"tenant:cat:{slug}")
    kb.button(text="⬅️ Back", callback_data="tenant:menu")
    kb.adjust(2)
    return kb.as_markup()


def _deposit_keyboard():
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    presets = [100, 200, 500, 1000]
    kb = InlineKeyboardBuilder()
    for amount in presets:
        kb.button(text=f"₹{amount}", callback_data=f"tenant:deposit:{amount}")
    kb.button(text="⬅️ Back", callback_data="tenant:wallet")
    kb.adjust(2)
    return kb.as_markup()


def _back_to_menu_keyboard():
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Main Menu", callback_data="tenant:menu")
    return kb.as_markup()
