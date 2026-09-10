"""
Bot message renderer — all user-facing text in one place.

Rules:
  - All messages use HTML parse mode
  - Emoji are wrapped in _e() so they degrade gracefully
  - STATUS_LABEL maps canonical status strings to display text
  - No business logic — formatting only
  - CMS blocks override any function here when present
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional


STATUS_LABEL: dict[str, str] = {
    "pending":     "⏳ Pending",
    "processing":  "⚙️ Processing",
    "in_progress": "🔄 In progress",
    "completed":   "✅ Completed",
    "partial":     "⚠️ Partial",
    "failed":      "❌ Failed",
    "cancelled":   "🚫 Cancelled",
    "refunded":    "💸 Refunded",
}


def _e(emoji: str, fallback: str = "") -> str:
    """Return emoji if non-empty after stripping whitespace, else fallback."""
    stripped = (emoji or "").strip()
    return stripped if stripped else fallback


def status_line(status: str) -> str:
    return STATUS_LABEL.get(status, status)


def welcome_message(
    first_name: str,
    balance: Decimal,
    is_premium: bool = False,
    cms_caption: Optional[str] = None,
) -> str:
    """Main menu / welcome message."""
    if cms_caption:
        try:
            return cms_caption.format(
                first_name=first_name,
                balance=f"₹{balance:.2f}",
            )
        except (KeyError, ValueError):
            pass  # fallback to default

    premium_badge = " 👑 <b>Premium</b>" if is_premium else ""
    return (
        f"👋 Welcome, <b>{first_name}</b>!{premium_badge}\n\n"
        f"💳 Balance: <b>₹{balance:.2f}</b>\n\n"
        f"Use the menu below to browse services and place orders."
    )


def service_detail(
    public_id: str,
    display_name: str,
    category: str,
    min_qty: int,
    max_qty: int,
    price_per_1000: Decimal,
    refill: bool,
    cancel: bool,
    description: Optional[str] = None,
) -> str:
    """Service detail card."""
    badges = []
    if refill: badges.append("♻️ Refill")
    if cancel: badges.append("❌ Cancel")
    badge_line = "  ".join(badges)

    lines = [
        f"📦 <b>{display_name}</b>",
        f"<code>{public_id}</code>  •  {category}",
        "",
        f"🔢 Quantity: {min_qty:,} – {max_qty:,}",
        f"💰 Price: <b>₹{price_per_1000:.4f}</b> per 1,000",
    ]
    if badge_line:
        lines.append(badge_line)
    if description:
        lines += ["", description]
    return "\n".join(lines)


def order_submitted(public_ref: str) -> str:
    return (
        f"✅ <b>Order placed!</b>\n\n"
        f"Ref: <code>{public_ref}</code>\n\n"
        f"Processing usually takes a few minutes. "
        f"Check the Orders tab to track progress."
    )


def order_detail(order) -> str:
    """Render order detail card from an Order ORM object."""
    lines = [
        f"📋 <b>Order {order.public_ref}</b>",
        f"Status: {status_line(order.status)}",
        "",
        f"Qty: {order.quantity:,}",
        f"Price: ₹{order.price_charged:.2f}",
    ]
    if order.remains is not None:
        lines.append(f"Remains: {order.remains:,}")
    if order.start_count is not None:
        lines.append(f"Start count: {order.start_count:,}")
    if order.link:
        lines.append(f"Link: {order.link}")
    return "\n".join(lines)


def profile_card(
    first_name: str,
    telegram_id: int,
    balance: Decimal,
    total_orders: int,
    is_premium: bool,
) -> str:
    premium_line = "\n👑 Premium member" if is_premium else ""
    return (
        f"👤 <b>Profile</b>{premium_line}\n\n"
        f"Name: {first_name}\n"
        f"ID: <code>{telegram_id}</code>\n\n"
        f"💳 Balance: <b>₹{balance:.2f}</b>\n"
        f"📦 Orders: {total_orders:,}"
    )


def insufficient_balance(
    required: Decimal,
    available: Decimal,
) -> str:
    shortfall = required - available
    return (
        f"❌ <b>Insufficient balance</b>\n\n"
        f"Required: ₹{required:.2f}\n"
        f"Available: ₹{available:.2f}\n"
        f"Shortfall: ₹{shortfall:.2f}\n\n"
        f"Please deposit funds to continue."
    )


def deposit_success(amount: Decimal, new_balance: Decimal) -> str:
    return (
        f"✅ <b>Deposit successful!</b>\n\n"
        f"Added: ₹{amount:.2f}\n"
        f"New balance: ₹{new_balance:.2f}"
    )


def pagination_footer(page: int, total_pages: int) -> str:
    if total_pages <= 1:
        return ""
    return f"Page {page} of {total_pages}"


def error_message(user_message: str) -> str:
    return f"❌ {user_message}"


def maintenance_message() -> str:
    return (
        "🔧 <b>Maintenance</b>\n\n"
        "The platform is temporarily unavailable. "
        "Please try again in a few minutes."
    )
