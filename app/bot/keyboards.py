"""
Bot keyboards — CB callback data namespace and all InlineKeyboardMarkup builders.

CB.pack() enforces the 64-byte Telegram limit on callback_data.
All callback data is namespaced via CB constants.
"""
from __future__ import annotations

from aiogram.utils.keyboard import InlineKeyboardBuilder


class CB:
    """Callback data constants and pack() helper."""
    # Navigation
    HOME       = "home"
    BACK       = "back"
    # Services
    CAT_LIST   = "cat_list"
    SVC_LIST   = "svc_list"
    SVC_DETAIL = "svc_detail"
    # Orders
    ORDER_LIST    = "ord_list"
    ORDER_DETAIL  = "ord_detail"
    ORDER_CONFIRM = "ord_confirm"
    ORDER_CANCEL  = "ord_cancel"
    ORDER_REFILL  = "ord_refill"
    # Wallet
    WALLET    = "wallet"
    DEPOSIT   = "deposit"
    DEP_AMT   = "dep_amt"
    # Profile
    PROFILE   = "profile"

    @staticmethod
    def pack(*parts) -> str:
        """
        Join callback data parts with ':' and enforce the 64-byte limit.
        Raises AssertionError if the result exceeds 64 bytes.
        """
        data = ":".join(str(p) for p in parts)
        assert len(data.encode()) <= 64, (
            f"callback_data exceeds 64 bytes: {data!r} ({len(data.encode())} bytes)"
        )
        return data


def main_menu_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text="🛒 Services",  callback_data=CB.CAT_LIST)
    kb.button(text="📋 Orders",    callback_data=CB.ORDER_LIST)
    kb.button(text="💳 Wallet",    callback_data=CB.WALLET)
    kb.button(text="👤 Profile",   callback_data=CB.PROFILE)
    kb.adjust(2)
    return kb.as_markup()


def categories_keyboard(categories: list) -> object:
    kb = InlineKeyboardBuilder()
    for cat in categories:
        kb.button(
            text=cat.name,
            callback_data=CB.pack(CB.SVC_LIST, cat.id, 1),
        )
    kb.button(text="🏠 Home", callback_data=CB.HOME)
    kb.adjust(2)
    return kb.as_markup()


def services_keyboard(services: list, page: int, total_pages: int, cat_id: int) -> object:
    kb = InlineKeyboardBuilder()
    for svc in services:
        kb.button(
            text=svc.display_name[:32],
            callback_data=CB.pack(CB.SVC_DETAIL, svc.public_id),
        )
    kb.adjust(1)

    nav = []
    if page > 1:
        nav.append(kb.button(text="◀ Prev", callback_data=CB.pack(CB.SVC_LIST, cat_id, page-1)))
    if page < total_pages:
        nav.append(kb.button(text="Next ▶", callback_data=CB.pack(CB.SVC_LIST, cat_id, page+1)))
    if nav:
        kb.adjust(len(nav))

    kb.button(text="⬅️ Categories", callback_data=CB.CAT_LIST)
    return kb.as_markup()


def service_detail_keyboard(public_id: str, refill: bool = False, cancel: bool = False) -> object:
    kb = InlineKeyboardBuilder()
    kb.button(text="🛒 Order now", callback_data=CB.pack(CB.ORDER_CONFIRM, public_id))
    if refill:
        kb.button(text="♻️ Refill", callback_data=CB.pack(CB.ORDER_REFILL, public_id))
    if cancel:
        kb.button(text="❌ Cancel", callback_data=CB.pack(CB.ORDER_CANCEL, public_id))
    kb.button(text="⬅️ Back", callback_data=CB.CAT_LIST)
    kb.adjust(1)
    return kb.as_markup()


def qty_suggestions_keyboard(suggestions: list[int], public_id: str) -> object:
    kb = InlineKeyboardBuilder()
    for qty in suggestions:
        kb.button(
            text=f"{qty:,}",
            callback_data=CB.pack(CB.ORDER_CONFIRM, public_id, qty),
        )
    kb.button(text="✏️ Custom", callback_data=CB.pack("qty_custom", public_id))
    kb.button(text="⬅️ Back", callback_data=CB.pack(CB.SVC_DETAIL, public_id))
    kb.adjust(2)
    return kb.as_markup()


def order_confirm_keyboard(public_id: str, idem_key: str) -> object:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Confirm", callback_data=CB.pack(CB.ORDER_CONFIRM, public_id, idem_key))
    kb.button(text="❌ Cancel",  callback_data=CB.HOME)
    kb.adjust(2)
    return kb.as_markup()


def orders_keyboard(orders: list, page: int, total_pages: int) -> object:
    kb = InlineKeyboardBuilder()
    for order in orders:
        from app.bot.messages import STATUS_LABEL
        label = STATUS_LABEL.get(order.status, order.status)
        kb.button(
            text=f"{order.public_ref} — {label}",
            callback_data=CB.pack(CB.ORDER_DETAIL, order.id[:8]),
        )
    kb.adjust(1)
    if page > 1:
        kb.button(text="◀ Prev", callback_data=CB.pack(CB.ORDER_LIST, page-1))
    if page < total_pages:
        kb.button(text="Next ▶", callback_data=CB.pack(CB.ORDER_LIST, page+1))
    kb.button(text="🏠 Home", callback_data=CB.HOME)
    return kb.as_markup()


def deposit_keyboard(presets: list[int] | None = None) -> object:
    kb = InlineKeyboardBuilder()
    for amount in (presets or [100, 200, 500, 1000, 2000, 5000]):
        kb.button(
            text=f"₹{amount:,}",
            callback_data=CB.pack(CB.DEP_AMT, amount),
        )
    kb.button(text="✏️ Custom amount", callback_data="dep_custom")
    kb.button(text="🏠 Home", callback_data=CB.HOME)
    kb.adjust(3)
    return kb.as_markup()


def back_to_home_keyboard() -> object:
    kb = InlineKeyboardBuilder()
    kb.button(text="🏠 Main Menu", callback_data=CB.HOME)
    return kb.as_markup()
