"""
Admin bot keyboards — all 25 admin panel sections.
Section 20 of blueprint.
"""
from __future__ import annotations
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import InlineKeyboardMarkup


class ACB:
    """Admin callback data namespace."""
    HOME         = "a:home"
    DASHBOARD    = "a:dashboard"
    USERS        = "a:users"
    USERS_SEARCH = "a:users:search"
    USERS_LIST   = "a:users:list"       # a:users:list:{page}
    USER_DETAIL  = "a:user:detail"      # a:user:detail:{user_id}
    USER_BAN     = "a:user:ban"         # a:user:ban:{user_id}
    USER_UNBAN   = "a:user:unban"       # a:user:unban:{user_id}
    USER_WALLET  = "a:user:wallet"      # a:user:wallet:{user_id}
    USER_CREDIT  = "a:user:credit"      # a:user:credit:{user_id}
    USER_DEBIT   = "a:user:debit"       # a:user:debit:{user_id}
    USER_ORDERS  = "a:user:orders"      # a:user:orders:{user_id}
    # Services
    SERVICES     = "a:services"
    SVC_LIST     = "a:svc:list"         # a:svc:list:{page}
    SVC_DETAIL   = "a:svc:detail"       # a:svc:detail:{svc_id}
    SVC_TOGGLE   = "a:svc:toggle"       # a:svc:toggle:{svc_id}
    SVC_EDIT     = "a:svc:edit"
    # Categories
    CATEGORIES   = "a:cats"
    CAT_LIST     = "a:cat:list"
    CAT_DETAIL   = "a:cat:detail"       # a:cat:detail:{cat_id}
    # Providers
    PROVIDERS    = "a:providers"
    PROV_LIST    = "a:prov:list"
    PROV_DETAIL  = "a:prov:detail"      # a:prov:detail:{prov_id}
    PROV_SYNC    = "a:prov:sync"        # a:prov:sync:{prov_id}
    PROV_TOGGLE  = "a:prov:toggle"      # a:prov:toggle:{prov_id}
    PROV_HEALTH  = "a:prov:health"
    PROV_BALANCE = "a:prov:balance"     # a:prov:balance:{prov_id}
    # Pricing
    PRICING      = "a:pricing"
    # Orders admin
    ORDERS       = "a:orders"
    ORD_LIST     = "a:ord:list"         # a:ord:list:{page}
    ORD_DETAIL   = "a:ord:detail"       # a:ord:detail:{short_id}
    ORD_REFUND   = "a:ord:refund"       # a:ord:refund:{order_id}
    ORD_REFUND_OK= "a:ord:refund_ok"
    # Payments
    PAYMENTS     = "a:payments"
    PAY_LIST     = "a:pay:list"
    # Wallets
    WALLETS      = "a:wallets"
    # Broadcasts
    BROADCAST    = "a:broadcast"
    BC_ALL       = "a:bc:all"
    BC_PREMIUM   = "a:bc:premium"
    BC_VIP       = "a:bc:vip"
    BC_INACTIVE  = "a:bc:inactive"
    # Kill switches
    KILLSWITCHES = "a:ks"
    KS_TOGGLE    = "a:ks:toggle"        # a:ks:toggle:{name}
    # Security
    SECURITY     = "a:security"
    # Audit
    AUDIT        = "a:audit"
    AUDIT_LIST   = "a:audit:list"       # a:audit:list:{page}
    # Analytics
    ANALYTICS    = "a:analytics"
    # Settings
    SETTINGS     = "a:settings"
    # Diagnostics
    DIAGNOSTICS  = "a:diag"
    # Premium admin
    PREMIUM      = "a:premium"
    PREM_GRANT   = "a:prem:grant"
    PREM_REVOKE  = "a:prem:revoke"
    # Maintenance
    MAINTENANCE  = "a:maint"
    # Feature flags
    FLAGS        = "a:flags"

    @staticmethod
    def pack(*parts) -> str:
        data = ":".join(str(p) for p in parts)
        assert len(data.encode()) <= 64, f"Admin callback_data too long: {data!r}"
        return data


def admin_main_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📊 Dashboard",     callback_data=ACB.DASHBOARD)
    kb.button(text="👥 Users",         callback_data=ACB.USERS)
    kb.button(text="🛍️ Services",      callback_data=ACB.SERVICES)
    kb.button(text="📂 Categories",    callback_data=ACB.CATEGORIES)
    kb.button(text="🖥️ Providers",     callback_data=ACB.PROVIDERS)
    kb.button(text="💰 Pricing",       callback_data=ACB.PRICING)
    kb.button(text="📦 Orders",        callback_data=ACB.ORDERS)
    kb.button(text="💳 Payments",      callback_data=ACB.PAYMENTS)
    kb.button(text="💵 Wallets",       callback_data=ACB.WALLETS)
    kb.button(text="👑 Premium",       callback_data=ACB.PREMIUM)
    kb.button(text="📢 Broadcast",     callback_data=ACB.BROADCAST)
    kb.button(text="🛠️ Maintenance",   callback_data=ACB.MAINTENANCE)
    kb.button(text="🚨 Kill Switches", callback_data=ACB.KILLSWITCHES)
    kb.button(text="🔐 Security",      callback_data=ACB.SECURITY)
    kb.button(text="📝 Audit Log",     callback_data=ACB.AUDIT)
    kb.button(text="📈 Analytics",     callback_data=ACB.ANALYTICS)
    kb.button(text="⚙️ Settings",      callback_data=ACB.SETTINGS)
    kb.button(text="🧪 Diagnostics",   callback_data=ACB.DIAGNOSTICS)
    kb.button(text="🚦 Feature Flags", callback_data=ACB.FLAGS)
    kb.adjust(2)
    return kb.as_markup()


def back_admin_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Admin Menu", callback_data=ACB.HOME)
    return kb.as_markup()


def user_action_keyboard(user_id: int, is_banned: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if is_banned:
        kb.button(text="✅ Unban",     callback_data=ACB.pack(ACB.USER_UNBAN,  user_id))
    else:
        kb.button(text="🚫 Ban",       callback_data=ACB.pack(ACB.USER_BAN,    user_id))
    kb.button(text="💳 Wallet",        callback_data=ACB.pack(ACB.USER_WALLET, user_id))
    kb.button(text="➕ Credit",        callback_data=ACB.pack(ACB.USER_CREDIT, user_id))
    kb.button(text="➖ Debit",         callback_data=ACB.pack(ACB.USER_DEBIT,  user_id))
    kb.button(text="📦 Orders",        callback_data=ACB.pack(ACB.USER_ORDERS, user_id))
    kb.button(text="⬅️ Back",         callback_data=ACB.USERS)
    kb.adjust(2)
    return kb.as_markup()


def kill_switch_keyboard(active_switches: dict) -> InlineKeyboardMarkup:
    from app.security.kill_switches import VALID_SWITCHES
    kb = InlineKeyboardBuilder()
    for name in sorted(VALID_SWITCHES):
        is_on  = name in active_switches
        status = "🔴 ON" if is_on else "🟢 OFF"
        label  = f"{status}  {name.replace('_', ' ').title()}"
        kb.button(text=label, callback_data=ACB.pack(ACB.KS_TOGGLE, name[:20]))
    kb.button(text="🔄 Refresh", callback_data=ACB.KILLSWITCHES)
    kb.button(text="⬅️ Admin",   callback_data=ACB.HOME)
    kb.adjust(1)
    return kb.as_markup()


def providers_keyboard(providers: list) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for p in providers:
        health = "🟢" if p.get("health") == "healthy" else "🔴"
        label  = f"{health} {p['name']}"
        kb.button(text=label, callback_data=ACB.pack(ACB.PROV_DETAIL, p["id"]))
    kb.button(text="⬅️ Admin", callback_data=ACB.HOME)
    kb.adjust(1)
    return kb.as_markup()


def provider_action_keyboard(prov_id: int, is_active: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    toggle = "🔴 Disable" if is_active else "🟢 Enable"
    kb.button(text=toggle,          callback_data=ACB.pack(ACB.PROV_TOGGLE,  prov_id))
    kb.button(text="🔄 Sync Now",   callback_data=ACB.pack(ACB.PROV_SYNC,   prov_id))
    kb.button(text="💰 Balance",    callback_data=ACB.pack(ACB.PROV_BALANCE, prov_id))
    kb.button(text="📊 Health",     callback_data=ACB.PROV_HEALTH)
    kb.button(text="⬅️ Providers",  callback_data=ACB.PROVIDERS)
    kb.adjust(2)
    return kb.as_markup()


def broadcast_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📢 All Users",        callback_data=ACB.BC_ALL)
    kb.button(text="👑 Premium Users",    callback_data=ACB.BC_PREMIUM)
    kb.button(text="💎 VIP Users",        callback_data=ACB.BC_VIP)
    kb.button(text="😴 Inactive 30d",     callback_data=ACB.BC_INACTIVE)
    kb.button(text="⬅️ Admin",            callback_data=ACB.HOME)
    kb.adjust(2)
    return kb.as_markup()


def confirm_keyboard(yes_cb: str, no_cb: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Yes, confirm", callback_data=yes_cb)
    kb.button(text="❌ No, cancel",   callback_data=no_cb)
    kb.adjust(2)
    return kb.as_markup()
