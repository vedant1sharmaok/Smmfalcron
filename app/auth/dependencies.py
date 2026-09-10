"""FastAPI auth dependencies."""
from __future__ import annotations
from fastapi import Depends, Header, HTTPException
from app.auth.sessions import get_session
from app.auth.permissions import Permission, has_permission
from app.core.exceptions import AuthError, PermissionDeniedError


async def get_current_user(
    authorization: str = Header(default=""),
    x_telegram_init_data: str = Header(default=""),
) -> dict:
    """
    Resolve the current authenticated user from session token or initData.
    Returns session dict with user_id, type, etc.
    """
    token = ""
    if authorization.startswith("Bearer "):
        token = authorization[7:]

    if not token:
        raise HTTPException(status_code=401, detail="Authentication required")

    session = await get_session(token)
    if session is None:
        raise HTTPException(status_code=401, detail="Session expired or invalid")

    return session


async def get_current_admin(
    authorization: str = Header(default=""),
) -> dict:
    """Verify admin JWT and return payload."""
    from app.auth.admin_credential_store import verify_admin_jwt
    from app.core.exceptions import AdminAuthError

    token = authorization[7:] if authorization.startswith("Bearer ") else ""
    if not token:
        raise HTTPException(status_code=401, detail="Admin authentication required")

    try:
        payload = verify_admin_jwt(token, require_full=True)
        return payload
    except AdminAuthError as e:
        raise HTTPException(status_code=401, detail=e.user_message)


def require_permission(permission: Permission):
    """Dependency factory — checks admin has the required permission."""
    async def _check(admin: dict = Depends(get_current_admin)) -> dict:
        role = admin.get("role", "")
        if not has_permission(role, permission):
            raise HTTPException(
                status_code=403,
                detail=f"Permission denied: {permission.value}",
            )
        return admin
    return _check
