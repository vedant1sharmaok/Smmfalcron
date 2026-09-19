"""
Bot keyboards — CB namespace + all InlineKeyboardMarkup builders.
Section 4.2, 5, 13, 15, 40 of blueprint.
Callbacks EDIT existing messages (Section 50.19).
CB.pack() enforces Telegram 64-byte callback_data limit.
"""
from __future__ import annotations
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import InlineKeyboardMarkup


class CB:
    """Callback data constants. All namespaced to avoid collisions."""
    HOME        = "home"
    BACK        = "back"
    # Policy
    POLICY_ACCEPT  = "policy:accept"
    POLICY_TOS     = "policy:tos"
    POLICY_PRIVACY = "policy:privacy"
    POLICY_REFUND  = "policy:refund"
    # Services
    CAT_LIST    = "cat:list"
    CAT_PAGE    = "cat:page"       # cat:page:{page}
    SVC_LIST    = "svc:list"       # svc:list:{cat_id}:{page}
    SVC_DETAIL  = "svc:detail"     # svc:detail:{public_id}
    SVC_ORDER   = "svc:order"      # svc:order:{public_id}
    # Orders
    ORD_LIST    = "ord:list"       # ord:list:{page}
    ORD_DETAIL  = "ord:detail"     # ord:detail:{order_id_short}
    ORD_REFRESH = "ord:refresh"    # ord:refresh:{order_id_short}
    ORD_CANCEL  = "ord:cancel"     # ord:cancel:{order_id_short}
    ORD_CONFIRM = "ord:confirm"
    # Refills
    REFILL_LIST   = "ref:list"
    REFILL_DO     = "ref:do"       # ref:do:{order_id_short}
    REFILL_CONFIRM= "ref:confirm"  # ref:confirm:{order_id_short}
    # Wallet / Deposit
    WALLET      = "wallet"
    DEPOSIT     = "dep:menu"
    DEP_AMT     = "dep:amt"        # dep:amt:{amount}
    DEP_CUSTOM  = "dep:custom"
    BALANCE     = "balance"
    # Profile / Stats
    PROFILE     = "profile"
    STATS       = "stats"
    # Premium
    PREMIUM     = "premium"
    PREMIUM_BUY = "prem:buy"       # prem:buy:{plan}
    # Rewards
    REWARDS     = "rewards"
    # Support / Settings / Policies
    SUPPORT     = "support"
    SETTINGS    = "settings"
    POLICIES    = "policies"
    POL_TOS     = "pol:tos"
    POL_PRIVACY = "pol:privacy"
    POL_REFUND  = "pol:refund"
    # Coupon
    COUPON_APPLY = "coupon:apply"

    @staticmethod
    def pack(*parts) -> str:
        data = ":".join(str(p) for p in parts)
        assert len(data.encode()) <= 64, f"callback_data {data!r} exceeds 64 bytes"
        return data

    @staticmethod
    def order_short(order_id: str) -> str:
        """Use first 8 chars of UUID as short key for callback data."""
        return order_id[:8]


# ── Policy gate ────────────────────────────────────────────────────────────────
def policy_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📜 Terms of Service",  callback_data=CB.POLICY_TOS)
    kb.button(text="🔒 Privacy Policy",    callback_data=CB.POLICY_PRIVACY)
    kb.button(text="💳 Refund Policy",     callback_data=CB.POLICY_REFUND)
    kb.button(text="✅ Accept & Continue", callback_data=CB.POLICY_ACCEPT)
    kb.adjust(1)
    return kb.as_markup()

def policy_view_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Back", callback_data=CB.HOME)
    return kb.as_markup()

def policy_accepted_keyboard() -> InlineKeyboardMarkup:
    return main_menu_keyboard()

# ── Main menu ──────────────────────────────────────────────────────────────────
def main_menu_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🛍️ Services",      callback_data=CB.CAT_LIST)
    kb.button(text="💳 Add Funds",     callback_data=CB.DEPOSIT)
    kb.button(text="📦 My Orders",     callback_data=CB.pack(CB.ORD_LIST, 1))
    kb.button(text="🔁 Refills",       callback_data=CB.REFILL_LIST)
    kb.button(text="👤 My Profile",    callback_data=CB.PROFILE)
    kb.button(text="📊 My Stats",      callback_data=CB.STATS)
    kb.button(text="💰 Balance",       callback_data=CB.BALANCE)
    kb.button(text="⭐ Premium",        callback_data=CB.PREMIUM)
    kb.button(text="🎁 Rewards",       callback_data=CB.REWARDS)
    kb.button(text="📞 Support",       callback_data=CB.SUPPORT)
    kb.button(text="⚙️ Settings",      callback_data=CB.SETTINGS)
    kb.button(text="📜 Policies",      callback_data=CB.POLICIES)
    kb.adjust(2)
    return kb.as_markup()

def back_home_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🏠 Main Menu", callback_data=CB.HOME)
    return kb.as_markup()

# ── Services ──────────────────────────────────────────────────────────────────
def categories_keyboard(categories: list, page: int = 1, total_pages: int = 1) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for cat in categories:
        cb = CB.pack(CB.SVC_LIST, cat["id"], 1)
        kb.button(text=cat["name"], callback_data=cb)
    kb.adjust(2)
    nav = []
    if page > 1:         nav.append(("◀ Prev", CB.pack(CB.CAT_PAGE, page - 1)))
    if page < total_pages: nav.append(("Next ▶", CB.pack(CB.CAT_PAGE, page + 1)))
    for label, cb in nav:
        kb.button(text=label, callback_data=cb)
    if nav: kb.adjust(2, *([1] * (len(categories) // 2 + 1)), len(nav))
    kb.button(text="🏠 Main Menu", callback_data=CB.HOME)
    kb.adjust(2)
    return kb.as_markup()

def services_keyboard(services: list, cat_id: int, page: int, total_pages: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for svc in services:
        label = svc["display_name"][:32]
        cb    = CB.pack(CB.SVC_DETAIL, svc["public_id"])
        kb.button(text=label, callback_data=cb)
    kb.adjust(1)
    if page > 1:
        kb.button(text="◀ Prev", callback_data=CB.pack(CB.SVC_LIST, cat_id, page - 1))
    if page < total_pages:
        kb.button(text="Next ▶", callback_data=CB.pack(CB.SVC_LIST, cat_id, page + 1))
    kb.button(text="⬅️ Categories", callback_data=CB.CAT_LIST)
    kb.button(text="🏠 Main Menu",  callback_data=CB.HOME)
    return kb.as_markup()

def service_detail_keyboard(public_id: str, refill: bool = False, cancel: bool = False) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🛒 Order Now", callback_data=CB.pack(CB.SVC_ORDER, public_id))
    kb.button(text="⬅️ Back",     callback_data=CB.CAT_LIST)
    kb.button(text="🏠 Main Menu", callback_data=CB.HOME)
    kb.adjust(1)
    return kb.as_markup()

def qty_suggestions_keyboard(suggestions: list[int], public_id: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for qty in suggestions:
        cb = CB.pack("ord:qty", public_id, qty)
        kb.button(text=f"{qty:,}", callback_data=cb)
    kb.button(text="✏️ Custom quantity", callback_data=CB.pack("ord:qty_custom", public_id))
    kb.button(text="⬅️ Back",           callback_data=CB.pack(CB.SVC_DETAIL, public_id))
    kb.adjust(2)
    return kb.as_markup()

def order_confirm_keyboard(idem_key: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Confirm Order", callback_data=CB.pack(CB.ORD_CONFIRM, idem_key[:20]))
    kb.button(text="❌ Cancel",        callback_data=CB.HOME)
    kb.adjust(2)
    return kb.as_markup()

# ── Orders ────────────────────────────────────────────────────────────────────
def orders_keyboard(orders: list, page: int, total_pages: int) -> InlineKeyboardMarkup:
    from app.bot.messages import STATUS_EMOJI
    kb = InlineKeyboardBuilder()
    for o in orders:
        emoji = STATUS_EMOJI.get(o["status"], "❓")
        label = f"{emoji} {o['public_ref']}"
        cb    = CB.pack(CB.ORD_DETAIL, o["id"][:8])
        kb.button(text=label, callback_data=cb)
    kb.adjust(1)
    if page > 1:
        kb.button(text="◀ Prev", callback_data=CB.pack(CB.ORD_LIST, page - 1))
    if page < total_pages:
        kb.button(text="Next ▶", callback_data=CB.pack(CB.ORD_LIST, page + 1))
    kb.button(text="🏠 Main Menu", callback_data=CB.HOME)
    return kb.as_markup()

def order_detail_keyboard(order_id: str, refill_eligible: bool,
                           cancel_eligible: bool) -> InlineKeyboardMarkup:
    short = order_id[:8]
    kb    = InlineKeyboardBuilder()
    kb.button(text="🔄 Refresh", callback_data=CB.pack(CB.ORD_REFRESH, short))
    if refill_eligible:
        kb.button(text="♻️ Refill", callback_data=CB.pack(CB.REFILL_DO, short))
    if cancel_eligible:
        kb.button(text="❌ Cancel", callback_data=CB.pack(CB.ORD_CANCEL, short))
    kb.button(text="⬅️ Orders",   callback_data=CB.pack(CB.ORD_LIST, 1))
    kb.button(text="🏠 Main Menu", callback_data=CB.HOME)
    kb.adjust(2)
    return kb.as_markup()

# ── Refills ───────────────────────────────────────────────────────────────────
def refills_keyboard(orders: list) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for o in orders:
        label = f"♻️ {o['public_ref']}"
        cb    = CB.pack(CB.REFILL_DO, o["id"][:8])
        kb.button(text=label, callback_data=cb)
    kb.adjust(1)
    kb.button(text="🏠 Main Menu", callback_data=CB.HOME)
    return kb.as_markup()

def refill_confirm_keyboard(short_id: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Confirm Refill", callback_data=CB.pack(CB.REFILL_CONFIRM, short_id))
    kb.button(text="❌ Cancel",         callback_data=CB.REFILL_LIST)
    kb.adjust(2)
    return kb.as_markup()

# ── Wallet / Deposit ──────────────────────────────────────────────────────────
def wallet_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Add Funds",    callback_data=CB.DEPOSIT)
    kb.button(text="📜 Transactions", callback_data=CB.BALANCE)
    kb.button(text="🏠 Main Menu",    callback_data=CB.HOME)
    kb.adjust(2)
    return kb.as_markup()

def deposit_keyboard(presets: list[int] | None = None) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for amt in (presets or [50, 100, 200, 500, 1000, 2000, 5000]):
        kb.button(text=f"₹{amt:,}", callback_data=CB.pack(CB.DEP_AMT, amt))
    kb.adjust(3)
    kb.button(text="✏️ Custom amount", callback_data=CB.DEP_CUSTOM)
    kb.button(text="⬅️ Back",          callback_data=CB.WALLET)
    kb.button(text="🏠 Main Menu",     callback_data=CB.HOME)
    return kb.as_markup()

# ── Premium ───────────────────────────────────────────────────────────────────
def premium_keyboard(is_premium: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if not is_premium:
        kb.button(text="👑 Premium — ₹499/mo",  callback_data=CB.pack(CB.PREMIUM_BUY, "premium"))
        kb.button(text="💎 VIP    — ₹999/mo",   callback_data=CB.pack(CB.PREMIUM_BUY, "vip"))
        kb.adjust(1)
    kb.button(text="🏠 Main Menu", callback_data=CB.HOME)
    return kb.as_markup()

# ── Policies ──────────────────────────────────────────────────────────────────
def policies_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📜 Terms of Service", callback_data=CB.POL_TOS)
    kb.button(text="🔒 Privacy Policy",   callback_data=CB.POL_PRIVACY)
    kb.button(text="💳 Refund Policy",    callback_data=CB.POL_REFUND)
    kb.button(text="🏠 Main Menu",        callback_data=CB.HOME)
    kb.adjust(1)
    return kb.as_markup()
