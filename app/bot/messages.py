"""
Bot messages — all customer-facing text.
HTML parse mode. Unicode emoji fallbacks. No business logic.
Section 4.2, 13, 27-28, 40 of blueprint.
"""
from __future__ import annotations
from decimal import Decimal
from typing import Optional

# ── Status system ──────────────────────────────────────────────────────────────
STATUS_EMOJI  = {"pending":"🟡","processing":"🔵","in_progress":"🔵",
                 "completed":"🟢","partial":"🟠","failed":"🔴",
                 "cancelled":"⚫","refunded":"💸","refill":"🔁"}
STATUS_LABEL  = {"pending":"Pending","processing":"Processing","in_progress":"In Progress",
                 "completed":"Completed","partial":"Partial","failed":"Failed",
                 "cancelled":"Cancelled","refunded":"Refunded","refill":"Refill"}

def status_line(status: str) -> str:
    return f"{STATUS_EMOJI.get(status,'❓')} {STATUS_LABEL.get(status, status.title())}"

# ── Policy gate (Section 4.1, 37) ─────────────────────────────────────────────
def policy_gate(first_name: str) -> str:
    return (
        f"🦅 <b>Welcome to SMM Falcron, {first_name}!</b>\n\n"
        f"Before you continue, please review and accept our policies:\n\n"
        f"📜 <b>Terms of Service</b> — rules governing platform use\n"
        f"🔒 <b>Privacy Policy</b> — how we handle your data\n"
        f"💳 <b>Refund Policy</b> — conditions for refunds\n\n"
        f"By tapping <b>✅ Accept &amp; Continue</b> you confirm you have read "
        f"and agree to all policies."
    )

# ── Main menu (Section 4.2, 40) ───────────────────────────────────────────────
def welcome_message(first_name: str, balance: Decimal, is_premium: bool = False,
                    order_count: int = 0, cms_caption: Optional[str] = None) -> str:
    if cms_caption:
        try:
            return cms_caption.format(first_name=first_name, balance=f"₹{balance:.2f}")
        except Exception:
            pass
    badge = " 👑" if is_premium else ""
    return (
        f"🦅 <b>SMM Falcron</b>{badge}\n\n"
        f"Welcome, <b>{first_name}</b>!\n\n"
        f"💳 Balance: <b>₹{balance:.2f}</b>\n"
        f"📦 Total Orders: <b>{order_count:,}</b>\n\n"
        f"Choose an option:"
    )

# ── Services (Section 5) ──────────────────────────────────────────────────────
def categories_header() -> str:
    return "🛍️ <b>Services</b>\n\nChoose a category:"

def services_list_header(cat_name: str, page: int, total_pages: int) -> str:
    pager = f" — Page {page}/{total_pages}" if total_pages > 1 else ""
    return f"📂 <b>{cat_name}</b>{pager}\n\nChoose a service:"

def service_card(public_id: str, display_name: str, category: str, min_qty: int,
                 max_qty: int, price_per_1000: Decimal, refill: bool, cancel: bool,
                 description: Optional[str] = None, requires_premium: bool = False) -> str:
    badges = []
    if refill:           badges.append("♻️ Refill")
    if cancel:           badges.append("❌ Cancel")
    if requires_premium: badges.append("👑 Premium only")
    lines = [
        f"📦 <b>{display_name}</b>",
        f"<code>{public_id}</code>  •  {category}",
        "",
        f"📊 Qty: <b>{min_qty:,}</b> – <b>{max_qty:,}</b>",
        f"💰 Price: <b>₹{price_per_1000:.4f}</b> / 1,000",
    ]
    if badges: lines.append("  ".join(badges))
    if description: lines += ["", f"ℹ️ {description}"]
    return "\n".join(lines)

# ── Dynamic order forms (Section 5.3) ─────────────────────────────────────────
def order_form_link_prompt(service_name: str, min_qty: int, max_qty: int,
                            price_per_1000: Decimal) -> str:
    return (
        f"🛍️ <b>{service_name}</b>\n\n"
        f"💰 ₹{price_per_1000:.4f} per 1,000 units\n"
        f"📊 Min: <b>{min_qty:,}</b>  Max: <b>{max_qty:,}</b>\n\n"
        f"Please enter the <b>link / URL</b> to target:"
    )

def order_form_comments_prompt(service_name: str) -> str:
    return f"💬 <b>{service_name}</b>\n\nEnter your <b>custom comments</b> (one per line):"

def order_form_usernames_prompt(service_name: str) -> str:
    return f"👥 <b>{service_name}</b>\n\nEnter <b>usernames/mentions</b> (one per line, without @):"

def order_form_qty_prompt(min_qty: int, max_qty: int) -> str:
    return (
        f"Enter the <b>quantity</b>:\n\n"
        f"Min: <b>{min_qty:,}</b>  •  Max: <b>{max_qty:,}</b>"
    )

def order_preview_card(service_name: str, quantity: int, link: str,
                        price: Decimal, coupon_discount: Optional[Decimal] = None) -> str:
    lines = [
        f"📋 <b>Order Preview</b>\n",
        f"Service: <b>{service_name}</b>",
        f"Quantity: <b>{quantity:,}</b>",
        f"Link: <code>{link}</code>",
    ]
    if coupon_discount and coupon_discount > 0:
        lines.append(f"🎁 Coupon discount: -₹{coupon_discount:.2f}")
    lines.append(f"\n💳 Total: <b>₹{price:.2f}</b>\n")
    lines.append("Confirm your order?")
    return "\n".join(lines)

def order_submitted(public_ref: str, service_name: str, quantity: int) -> str:
    return (
        f"✅ <b>Order Placed!</b>\n\n"
        f"Ref: <code>{public_ref}</code>\n"
        f"Service: {service_name}\n"
        f"Quantity: {quantity:,}\n\n"
        f"⏱ Processing usually takes a few minutes.\n"
        f"Use 📦 <b>My Orders</b> to track progress."
    )

# ── Orders (Section 13) ───────────────────────────────────────────────────────
def orders_header(count: int, page: int = 1, total_pages: int = 1) -> str:
    if count == 0:
        return "📦 <b>My Orders</b>\n\nYou haven't placed any orders yet.\n\nTap 🛍️ <b>Services</b> to get started."
    pager = f"  Page {page}/{total_pages}" if total_pages > 1 else ""
    return f"📦 <b>My Orders</b>{pager}\n\n{count:,} order(s):"

def order_detail_card(public_ref: str, service_name: str, status: str,
                       quantity: int, price: Decimal, link: Optional[str],
                       remains: Optional[int], start_count: Optional[int],
                       created_at: str, refill_eligible: bool, cancel_eligible: bool) -> str:
    lines = [
        f"📦 <b>Order Details</b>", "",
        f"📌 Ref: <code>{public_ref}</code>",
        f"🔖 Status: {status_line(status)}",
        f"🛍️ Service: <b>{service_name}</b>",
        f"📊 Quantity: <b>{quantity:,}</b>",
        f"💳 Price: <b>₹{price:.2f}</b>",
    ]
    if link:        lines.append(f"🔗 Link: <code>{link}</code>")
    if start_count is not None: lines.append(f"📈 Start count: {start_count:,}")
    if remains is not None:     lines.append(f"⏳ Remains: {remains:,}")
    lines += ["", f"📅 {created_at}"]
    actions = []
    if refill_eligible:  actions.append("♻️ Refill available")
    if cancel_eligible:  actions.append("❌ Cancel available")
    if actions: lines += [""] + actions
    return "\n".join(lines)

# ── Wallet / Deposit ──────────────────────────────────────────────────────────
def wallet_card(balance: Decimal) -> str:
    return (
        f"💳 <b>Wallet</b>\n\n"
        f"Balance: <b>₹{balance:.2f}</b>\n\n"
        f"Choose an action:"
    )

def deposit_prompt() -> str:
    return (
        f"💳 <b>Add Funds</b>\n\n"
        f"Select an amount or enter a custom amount:\n\n"
        f"Min: ₹10  •  Max: ₹1,00,000"
    )

def deposit_custom_prompt() -> str:
    return "Enter the amount in ₹ you want to deposit (min ₹10):"

def deposit_pending(amount: Decimal, provider: str) -> str:
    return (
        f"⏳ <b>Payment Initiated</b>\n\n"
        f"Amount: <b>₹{amount:.2f}</b>\n"
        f"Provider: {provider.title()}\n\n"
        f"Complete the payment using the link below.\n"
        f"Your balance will update automatically once confirmed."
    )

def deposit_success(amount: Decimal, new_balance: Decimal) -> str:
    return (
        f"✅ <b>Deposit Successful!</b>\n\n"
        f"Added: <b>₹{amount:.2f}</b>\n"
        f"New balance: <b>₹{new_balance:.2f}</b>"
    )

def insufficient_balance(required: Decimal, available: Decimal) -> str:
    return (
        f"❌ <b>Insufficient Balance</b>\n\n"
        f"Required:   ₹{required:.2f}\n"
        f"Available:  ₹{available:.2f}\n"
        f"Shortfall:  ₹{(required - available):.2f}\n\n"
        f"Tap 💳 <b>Add Funds</b> to deposit."
    )

def balance_ledger(balance: Decimal, total_deposited: Decimal,
                    total_spent: Decimal, total_refunded: Decimal) -> str:
    return (
        f"💰 <b>Balance Summary</b>\n\n"
        f"Current balance: <b>₹{balance:.2f}</b>\n\n"
        f"📊 Ledger:\n"
        f"  Total deposited: ₹{total_deposited:.2f}\n"
        f"  Total spent:     ₹{total_spent:.2f}\n"
        f"  Total refunded:  ₹{total_refunded:.2f}"
    )

# ── Refills (Section 15) ──────────────────────────────────────────────────────
def refills_header(count: int) -> str:
    if count == 0:
        return (
            "🔁 <b>Refills</b>\n\n"
            "No orders are currently eligible for refill.\n\n"
            "Refills are available on completed orders for services that support it."
        )
    return f"🔁 <b>Refills</b>\n\n{count} order(s) eligible for refill:"

def refill_confirm(public_ref: str, service_name: str) -> str:
    return (
        f"🔁 <b>Request Refill?</b>\n\n"
        f"Order: <code>{public_ref}</code>\n"
        f"Service: {service_name}\n\n"
        f"A refill will be submitted to the provider. Confirm?"
    )

def refill_submitted(public_ref: str) -> str:
    return (
        f"✅ <b>Refill Submitted</b>\n\n"
        f"Order: <code>{public_ref}</code>\n\n"
        f"Your refill is being processed. We'll notify you when it completes."
    )

# ── Profile (Section 22, 36) ──────────────────────────────────────────────────
def profile_card(first_name: str, username: Optional[str], telegram_id: int,
                  balance: Decimal, is_premium: bool, plan: str, member_since: str) -> str:
    badge  = f"\n👑 <b>Premium</b> — {plan.title()}" if is_premium else ""
    handle = f"@{username}" if username else f"ID: <code>{telegram_id}</code>"
    return (
        f"👤 <b>My Profile</b>{badge}\n\n"
        f"Name: <b>{first_name}</b>\n"
        f"User: {handle}\n"
        f"Member since: {member_since}\n\n"
        f"💳 Balance: <b>₹{balance:.2f}</b>"
    )

# ── Stats (Section 36) ────────────────────────────────────────────────────────
def stats_card(total_orders: int, completed: int, processing: int, failed: int,
                total_spent: Decimal, total_deposited: Decimal, total_refunded: Decimal,
                refills_used: int, fav_category: Optional[str] = None) -> str:
    lines = [
        f"📊 <b>My Stats</b>\n",
        f"📦 <b>Orders</b>",
        f"  Total:      {total_orders:,}",
        f"  🟢 Done:    {completed:,}",
        f"  🔵 Active:  {processing:,}",
        f"  🔴 Failed:  {failed:,}\n",
        f"💰 <b>Finance</b>",
        f"  Deposited:  ₹{total_deposited:.2f}",
        f"  Spent:      ₹{total_spent:.2f}",
        f"  Refunded:   ₹{total_refunded:.2f}\n",
        f"🔁 Refills used: {refills_used:,}",
    ]
    if fav_category: lines.append(f"❤️ Favourite: {fav_category}")
    return "\n".join(lines)

# ── Premium (Section 50.1) ────────────────────────────────────────────────────
def premium_status(is_premium: bool, plan: str, expires: Optional[str] = None) -> str:
    if is_premium:
        exp = f"\n📅 Expires: {expires}" if expires else " (Lifetime)"
        return (
            f"👑 <b>Premium — {plan.title()}</b>{exp}\n\n"
            f"Your active benefits:\n"
            f"✅ Higher order quantity limits\n"
            f"✅ Priority order processing\n"
            f"✅ Access to exclusive services\n"
            f"✅ Discounted pricing on all orders"
        )
    return (
        f"⭐ <b>Upgrade to Premium</b>\n\n"
        f"Unlock powerful benefits:\n\n"
        f"👑 <b>Premium</b> — Higher limits, 10% off all orders\n"
        f"💎 <b>VIP</b>     — Max limits, 20% off, top priority\n\n"
        f"Choose a plan to upgrade:"
    )

# ── Rewards / Referral ────────────────────────────────────────────────────────
def rewards_card(referral_code: str, referrals: int, reward_balance: Decimal) -> str:
    return (
        f"🎁 <b>Rewards &amp; Referrals</b>\n\n"
        f"Your code: <code>{referral_code}</code>\n\n"
        f"👥 Referrals: {referrals:,}\n"
        f"💰 Reward balance: ₹{reward_balance:.2f}\n\n"
        f"Share your code and earn <b>₹10</b> for every friend who makes their first deposit!"
    )

# ── Support ───────────────────────────────────────────────────────────────────
def support_card(support_username: str = "") -> str:
    contact = f"\n\nContact: @{support_username}" if support_username else ""
    return (
        f"📞 <b>Support</b>{contact}\n\n"
        f"Need help with an order? Please include your order reference number "
        f"(e.g. <code>ORD-20240813-ABCD1234</code>) in your message.\n\n"
        f"We typically respond within a few hours."
    )

# ── Settings ──────────────────────────────────────────────────────────────────
def settings_card() -> str:
    return "⚙️ <b>Settings</b>\n\nManage your account preferences:"

# ── Policies ─────────────────────────────────────────────────────────────────
TERMS_TEXT = (
    "📜 <b>Terms of Service</b>\n\n"
    "• Use this platform lawfully and responsibly.\n"
    "• Delivery times are estimates — not guaranteed.\n"
    "• Services cannot be guaranteed to remain permanently.\n"
    "• Refunds processed per our Refund Policy.\n"
    "• We reserve the right to refuse service.\n"
    "• Accounts violating these terms may be suspended."
)

PRIVACY_TEXT = (
    "🔒 <b>Privacy Policy</b>\n\n"
    "We collect only what is necessary to operate:\n"
    "• Telegram ID and display name (from Telegram)\n"
    "• Order history and wallet transactions\n"
    "• Payment records for reconciliation\n\n"
    "We never sell your data. Sensitive data is encrypted at rest."
)

REFUND_TEXT = (
    "💳 <b>Refund Policy</b>\n\n"
    "• Refunds are issued for orders that fail to start.\n"
    "• Partial refunds for partial delivery where supported.\n"
    "• No refunds after order begins for drop-based services.\n"
    "• Refunds are credited to your wallet balance.\n"
    "• Contact support with your order reference for refund requests."
)

# ── Generic errors / maintenance ──────────────────────────────────────────────
def error_card(user_message: str) -> str:
    return f"❌ {user_message}"

def maintenance_card() -> str:
    return (
        "🔧 <b>Maintenance</b>\n\n"
        "This feature is temporarily unavailable.\n"
        "Please try again in a few minutes."
    )

def pagination_note(page: int, total_pages: int) -> str:
    return f"\n\nPage {page} of {total_pages}" if total_pages > 1 else ""
