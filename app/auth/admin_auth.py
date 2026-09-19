"""
Admin auth — re-exports from admin_credential_store for backwards compatibility.
Full implementation lives in app/auth/admin_credential_store.py.
"""
from app.auth.admin_credential_store import (
    authenticate_admin,
    verify_admin_jwt,
    verify_admin_totp,
    setup_admin_totp,
    confirm_admin_totp,
    create_admin,
    rotate_admin_password,
    deactivate_admin,
)

__all__ = [
    "authenticate_admin",
    "verify_admin_jwt",
    "verify_admin_totp",
    "setup_admin_totp",
    "confirm_admin_totp",
    "create_admin",
    "rotate_admin_password",
    "deactivate_admin",
]
