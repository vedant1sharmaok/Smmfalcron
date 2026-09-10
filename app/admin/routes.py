"""
Admin API routes — requires admin JWT with appropriate permissions.

Endpoints:
  POST   /admin/auth/login           password + partial token
  POST   /admin/auth/totp            TOTP → full JWT
  POST   /admin/auth/setup-totp      generate TOTP secret
  POST   /admin/auth/confirm-totp    activate TOTP
  GET    /admin/users                list users
  GET    /admin/users/{id}           get user
  POST   /admin/users/{id}/ban       ban user
  POST   /admin/users/{id}/unban     unban user
  GET    /admin/wallet/{user_id}     wallet + transactions
  POST   /admin/wallet/{user_id}/adjust  credit/debit
  GET    /admin/orders               list orders
  POST   /admin/orders/{id}/refund   refund order
  GET    /admin/services             list services
  POST   /admin/services             create service
  PATCH  /admin/services/{id}        update service
  GET    /admin/kill-switches        list all switches + status
  POST   /admin/kill-switches/{name}/activate
  DELETE /admin/kill-switches/{name}
  GET    /admin/audit-log            paginated audit log
  POST   /admin/broadcasts           create broadcast
  GET    /admin/broadcasts/{id}/stats
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth.dependencies import require_permission
from app.auth.permissions import Permission
from app.core.database import AsyncSessionLocal
from app.core.logging import get_logger

logger = get_logger(__name__)
router = APIRouter(tags=["admin"])


# ── Auth ───────────────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    username: str
    password: str

class TOTPRequest(BaseModel):
    partial_token: str
    code: str


@router.post("/auth/login")
async def admin_login(req: LoginRequest):
    from app.auth.admin_credential_store import authenticate_admin
    from app.core.exceptions import InvalidCredentialsError, AdminLockedError
    async with AsyncSessionLocal() as db:
        try:
            admin, token = await authenticate_admin(db, req.username, req.password)
            return {"token": token, "totp_required": admin.totp_enabled}
        except (InvalidCredentialsError, AdminLockedError) as e:
            raise HTTPException(status_code=401, detail=e.user_message)


@router.post("/auth/totp")
async def admin_totp(req: TOTPRequest):
    from app.auth.admin_credential_store import verify_admin_totp
    from app.core.exceptions import AdminAuthError, InvalidTOTPError
    async with AsyncSessionLocal() as db:
        try:
            token = await verify_admin_totp(db, req.partial_token, req.code)
            return {"token": token}
        except (AdminAuthError, InvalidTOTPError) as e:
            raise HTTPException(status_code=401, detail=e.user_message)


# ── Users ──────────────────────────────────────────────────────────────────────

@router.get("/users")
async def list_users(
    page: int = 1,
    admin=Depends(require_permission(Permission.VIEW_USERS)),
):
    from sqlalchemy import select
    from app.core.models import User
    async with AsyncSessionLocal() as db:
        users = list((await db.execute(
            select(User).offset((page-1)*50).limit(50).order_by(User.id.desc())
        )).scalars().all())
    return {"items": [
        {"id": u.id, "telegram_id": u.telegram_id, "first_name": u.first_name,
         "username": u.username, "is_active": u.is_active, "is_banned": u.is_banned,
         "is_premium": u.is_premium, "created_at": u.created_at.isoformat() if u.created_at else None}
        for u in users
    ], "page": page}


@router.post("/users/{user_id}/ban")
async def ban_user(
    user_id: int,
    admin=Depends(require_permission(Permission.BAN_USER)),
):
    from app.core.models import User
    async with AsyncSessionLocal() as db:
        user = await db.get(User, user_id)
        if not user: raise HTTPException(status_code=404, detail="User not found")
        user.is_banned = True
        await db.commit()
    return {"ok": True}


@router.post("/users/{user_id}/unban")
async def unban_user(
    user_id: int,
    admin=Depends(require_permission(Permission.UNBAN_USER)),
):
    from app.core.models import User
    async with AsyncSessionLocal() as db:
        user = await db.get(User, user_id)
        if not user: raise HTTPException(status_code=404, detail="User not found")
        user.is_banned = False
        await db.commit()
    return {"ok": True}


# ── Wallet ─────────────────────────────────────────────────────────────────────

class WalletAdjustRequest(BaseModel):
    amount: str
    tx_type: str = "ADMIN_CREDIT"
    reason: str  = ""

@router.post("/wallet/{user_id}/adjust")
async def adjust_wallet(
    user_id: int,
    req: WalletAdjustRequest,
    admin=Depends(require_permission(Permission.ADJUST_WALLET)),
):
    import uuid
    from decimal import Decimal
    from app.wallet.ledger import credit, debit, TxType
    amount = Decimal(req.amount)
    idem   = str(uuid.uuid4())
    async with AsyncSessionLocal() as db:
        try:
            tx_type = TxType[req.tx_type]
        except KeyError:
            raise HTTPException(status_code=422, detail=f"Unknown tx_type: {req.tx_type}")
        if amount > 0:
            await credit(db=db, user_id=user_id, amount=amount, tx_type=tx_type,
                         idempotency_key=idem, reason=req.reason or "Admin adjustment")
        else:
            await debit(db=db, user_id=user_id, amount=-amount, tx_type=tx_type,
                        idempotency_key=idem, reason=req.reason or "Admin adjustment")
        await db.commit()
    return {"ok": True}


# ── Orders ────────────────────────────────────────────────────────────────────

@router.get("/orders")
async def list_all_orders(
    page: int = 1,
    admin=Depends(require_permission(Permission.VIEW_ALL_ORDERS)),
):
    from sqlalchemy import select
    from app.core.models import Order
    async with AsyncSessionLocal() as db:
        orders = list((await db.execute(
            select(Order).offset((page-1)*50).limit(50).order_by(Order.created_at.desc())
        )).scalars().all())
    return {"items": [
        {"id": o.id, "public_ref": o.public_ref, "user_id": o.user_id,
         "status": o.status, "quantity": o.quantity,
         "price_charged": str(o.price_charged), "source": o.source,
         "created_at": o.created_at.isoformat() if o.created_at else None}
        for o in orders
    ], "page": page}


@router.post("/orders/{order_id}/refund")
async def refund_order_admin(
    order_id: str,
    admin=Depends(require_permission(Permission.REFUND_ORDER)),
):
    from app.core.models import Order
    from app.orders.engine import refund_order
    async with AsyncSessionLocal() as db:
        order = await db.get(Order, order_id)
        if not order: raise HTTPException(status_code=404, detail="Order not found")
        await refund_order(db=db, order=order, actor_id=admin["sub"], reason="Admin refund")
        await db.commit()
    return {"ok": True}


# ── Kill switches ─────────────────────────────────────────────────────────────

class KSActivateRequest(BaseModel):
    reason: str
    ttl_s: int | None = None

@router.get("/kill-switches")
async def get_kill_switches(admin=Depends(require_permission(Permission.MANAGE_KILL_SWITCHES))):
    from app.security.kill_switches import get_all_active, VALID_SWITCHES
    active = await get_all_active()
    return {"switches": {name: {"active": name in active, **(active.get(name) or {})} for name in VALID_SWITCHES}}

@router.post("/kill-switches/{name}/activate")
async def activate_kill_switch(
    name: str, req: KSActivateRequest,
    admin=Depends(require_permission(Permission.MANAGE_KILL_SWITCHES)),
):
    from app.security.kill_switches import activate, VALID_SWITCHES
    if name not in VALID_SWITCHES: raise HTTPException(status_code=404, detail="Unknown switch")
    await activate(name, req.reason, actor_id=admin["sub"], ttl_s=req.ttl_s)
    return {"ok": True}

@router.delete("/kill-switches/{name}")
async def deactivate_kill_switch(
    name: str,
    admin=Depends(require_permission(Permission.MANAGE_KILL_SWITCHES)),
):
    from app.security.kill_switches import deactivate, VALID_SWITCHES
    if name not in VALID_SWITCHES: raise HTTPException(status_code=404, detail="Unknown switch")
    await deactivate(name, actor_id=admin["sub"])
    return {"ok": True}


# ── Audit log ─────────────────────────────────────────────────────────────────

@router.get("/audit-log")
async def get_audit_log(
    page: int = 1,
    admin=Depends(require_permission(Permission.VIEW_AUDIT_LOG)),
):
    from sqlalchemy import select
    from app.core.models import AuditLog
    async with AsyncSessionLocal() as db:
        entries = list((await db.execute(
            select(AuditLog).offset((page-1)*100).limit(100).order_by(AuditLog.id.desc())
        )).scalars().all())
    return {"items": [
        {"id": e.id, "actor_id": e.actor_id, "action": e.action,
         "resource": e.resource, "resource_id": e.resource_id,
         "created_at": e.created_at.isoformat() if e.created_at else None}
        for e in entries
    ], "page": page}


# ── Broadcasts ────────────────────────────────────────────────────────────────

class BroadcastRequest(BaseModel):
    title:   str
    message: str
    segment: str = "all"

@router.post("/broadcasts")
async def create_broadcast_admin(
    req: BroadcastRequest,
    admin=Depends(require_permission(Permission.SEND_BROADCAST)),
):
    from app.cms.service import create_broadcast
    async with AsyncSessionLocal() as db:
        bc = await create_broadcast(db=db, title=req.title, message=req.message,
                                    segment=req.segment, actor_id=admin["sub"])
        await db.commit()
    return {"broadcast_id": bc.id, "status": bc.status}
