"""RBAC permission system — roles and permission constants."""
from __future__ import annotations
from enum import Enum


class Permission(str, Enum):
    # User management
    VIEW_USERS        = "view_users"
    BAN_USER          = "ban_user"
    UNBAN_USER        = "unban_user"
    VIEW_USER_WALLETS = "view_user_wallets"
    ADJUST_WALLET     = "adjust_wallet"
    # Orders
    VIEW_ALL_ORDERS   = "view_all_orders"
    REFUND_ORDER      = "refund_order"
    # Services
    MANAGE_SERVICES   = "manage_services"
    MANAGE_PROVIDERS  = "manage_providers"
    TRIGGER_SYNC      = "trigger_sync"
    # Pricing
    MANAGE_PRICING    = "manage_pricing"
    # Security
    MANAGE_KILL_SWITCHES = "manage_kill_switches"
    VIEW_AUDIT_LOG    = "view_audit_log"
    VIEW_SECURITY_EVENTS = "view_security_events"
    # Tenants
    MANAGE_TENANTS    = "manage_tenants"
    # Premium / CMS
    MANAGE_PREMIUM    = "manage_premium"
    MANAGE_CMS        = "manage_cms"
    SEND_BROADCAST    = "send_broadcast"
    # Admin management
    MANAGE_ADMINS     = "manage_admins"


# Role → permission set
ROLE_PERMISSIONS: dict[str, set[Permission]] = {
    "viewer": {
        Permission.VIEW_USERS,
        Permission.VIEW_ALL_ORDERS,
        Permission.VIEW_AUDIT_LOG,
    },
    "support": {
        Permission.VIEW_USERS,
        Permission.VIEW_ALL_ORDERS,
        Permission.VIEW_USER_WALLETS,
        Permission.REFUND_ORDER,
        Permission.VIEW_AUDIT_LOG,
    },
    "operator": {
        Permission.VIEW_USERS,
        Permission.BAN_USER,
        Permission.UNBAN_USER,
        Permission.VIEW_ALL_ORDERS,
        Permission.VIEW_USER_WALLETS,
        Permission.ADJUST_WALLET,
        Permission.REFUND_ORDER,
        Permission.MANAGE_SERVICES,
        Permission.TRIGGER_SYNC,
        Permission.MANAGE_CMS,
        Permission.VIEW_AUDIT_LOG,
    },
    "admin": {p for p in Permission} - {Permission.MANAGE_ADMINS},
    "superadmin": {p for p in Permission},
}


def has_permission(role: str, permission: Permission) -> bool:
    """Return True if the given role includes the specified permission."""
    return permission in ROLE_PERMISSIONS.get(role, set())


def get_permissions(role: str) -> set[Permission]:
    """Return all permissions for a role."""
    return ROLE_PERMISSIONS.get(role, set())
