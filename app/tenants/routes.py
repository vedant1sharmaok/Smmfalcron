"""
Tenant admin routes + webhook receiver.

Admin routes (require superadmin permission):
  POST   /admin/tenants                   create tenant
  GET    /admin/tenants                   list tenants
  GET    /admin/tenants/{id}              get tenant detail
  PATCH  /admin/tenants/{id}              update tenant
  POST   /admin/tenants/{id}/activate     activate + start bot
  POST   /admin/tenants/{id}/suspend      suspend + stop bot
  DELETE /admin/tenants/{id}              soft-delete tenant
  POST   /admin/tenants/{id}/rotate-token rotate bot token
  GET    /admin/tenants/{id}/stats        usage statistics

Webhook receiver (public — verified by secret token header):
  POST   /bots/{slug}/webhook             Telegram update delivery
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.auth.dependencies import get_current_admin, require_permission
from app.auth.permissions import Permission
from app.core.database import AsyncSessionLocal
from app.core.exceptions import TenantError, TenantNotFoundError, ValidationError
from app.core.logging import get_logger
from app.tenants import service as tenant_svc
from app.tenants.bot_manager import tenant_bot_manager

logger = get_logger(__name__)

router      = APIRouter()
bot_webhook = APIRouter()


# ── Request / Response schemas ─────────────────────────────────────────────────

class TenantCreateRequest(BaseModel):
    bot_token:    str = Field(min_length=40, max_length=60)
    display_name: str = Field(min_length=1,  max_length=128)
    slug:         str = Field(min_length=3,  max_length=32)
    plan:         Literal["basic", "pro", "enterprise"] = "basic"


class TenantUpdateRequest(BaseModel):
    display_name: str | None  = Field(default=None, min_length=1, max_length=128)
    plan:         Literal["basic", "pro", "enterprise"] | None = None
    config:       dict | None = None


class TenantOut(BaseModel):
    id:            int
    slug:          str
    display_name:  str
    plan:          str
    is_active:     bool
    bot_username:  str | None
    owner_user_id: int
    created_at:    str

    # bot_token is NEVER in the response — not even as a masked field
    model_config = {"from_attributes": True}

    def model_post_init(self, __context: Any) -> None:
        # Belt-and-suspenders: ensure bot_token_enc never leaks
        for field in ("bot_token_enc", "bot_token", "webhook_secret"):
            if hasattr(self, field):
                object.__delattr__(self, field)


class TenantRotateTokenRequest(BaseModel):
    new_bot_token: str = Field(min_length=40, max_length=60)


class TenantSuspendRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=255)


# ── Admin routes ───────────────────────────────────────────────────────────────

@router.post("/tenants", response_model=TenantOut)
async def create_tenant(
    req: TenantCreateRequest,
    admin=Depends(require_permission(Permission.MANAGE_TENANTS)),
):
    async with AsyncSessionLocal() as db:
        try:
            tenant = await tenant_svc.create_tenant(
                db=db,
                owner_user_id=admin["sub"],
                bot_token=req.bot_token,
                display_name=req.display_name,
                slug=req.slug,
                plan=req.plan,
                actor_id=admin["sub"],
            )
            await db.commit()
            return _to_out(tenant)
        except ValidationError as e:
            raise HTTPException(status_code=422, detail=e.user_message)


@router.get("/tenants", response_model=dict)
async def list_tenants(
    page: int = 1,
    page_size: int = 20,
    owner_user_id: int | None = None,
    is_active: bool | None = None,
    admin=Depends(require_permission(Permission.MANAGE_TENANTS)),
):
    async with AsyncSessionLocal() as db:
        items, total = await tenant_svc.list_tenants(
            db=db,
            owner_user_id=owner_user_id,
            is_active=is_active,
            page=page,
            page_size=page_size,
        )
        return {
            "items":     [_to_out(t) for t in items],
            "total":     total,
            "page":      page,
            "page_size": page_size,
        }


@router.get("/tenants/{tenant_id}", response_model=TenantOut)
async def get_tenant(
    tenant_id: int,
    admin=Depends(require_permission(Permission.MANAGE_TENANTS)),
):
    async with AsyncSessionLocal() as db:
        try:
            tenant = await tenant_svc.get_tenant(db, tenant_id)
            return _to_out(tenant)
        except TenantNotFoundError as e:
            raise HTTPException(status_code=404, detail=e.user_message)


@router.patch("/tenants/{tenant_id}", response_model=TenantOut)
async def update_tenant(
    tenant_id: int,
    req: TenantUpdateRequest,
    admin=Depends(require_permission(Permission.MANAGE_TENANTS)),
):
    updates = {k: v for k, v in req.model_dump().items() if v is not None}
    async with AsyncSessionLocal() as db:
        try:
            tenant = await tenant_svc.update_tenant(
                db=db, tenant_id=tenant_id, updates=updates, actor_id=admin["sub"]
            )
            await db.commit()
            return _to_out(tenant)
        except (TenantNotFoundError, ValidationError) as e:
            raise HTTPException(status_code=422, detail=e.user_message)


@router.post("/tenants/{tenant_id}/activate", response_model=TenantOut)
async def activate_tenant(
    tenant_id: int,
    admin=Depends(require_permission(Permission.MANAGE_TENANTS)),
):
    async with AsyncSessionLocal() as db:
        try:
            tenant = await tenant_svc.activate_tenant(
                db=db, tenant_id=tenant_id, actor_id=admin["sub"]
            )
            await db.commit()
            # Start the bot instance
            await tenant_bot_manager.start_tenant(tenant)
            return _to_out(tenant)
        except (TenantNotFoundError, TenantError, ValidationError) as e:
            raise HTTPException(status_code=422, detail=str(e.user_message))


@router.post("/tenants/{tenant_id}/suspend")
async def suspend_tenant(
    tenant_id: int,
    req: TenantSuspendRequest,
    admin=Depends(require_permission(Permission.MANAGE_TENANTS)),
):
    async with AsyncSessionLocal() as db:
        try:
            await tenant_svc.suspend_tenant(
                db=db, tenant_id=tenant_id, reason=req.reason, actor_id=admin["sub"]
            )
            await db.commit()
            await tenant_bot_manager.stop_tenant(tenant_id)
            return {"ok": True}
        except TenantNotFoundError as e:
            raise HTTPException(status_code=404, detail=e.user_message)


@router.delete("/tenants/{tenant_id}")
async def delete_tenant(
    tenant_id: int,
    admin=Depends(require_permission(Permission.MANAGE_TENANTS)),
):
    async with AsyncSessionLocal() as db:
        try:
            await tenant_svc.delete_tenant(
                db=db, tenant_id=tenant_id, actor_id=admin["sub"]
            )
            await db.commit()
            await tenant_bot_manager.stop_tenant(tenant_id, delete_webhook=True)
            return {"ok": True}
        except TenantNotFoundError as e:
            raise HTTPException(status_code=404, detail=e.user_message)


@router.post("/tenants/{tenant_id}/rotate-token")
async def rotate_token(
    tenant_id: int,
    req: TenantRotateTokenRequest,
    admin=Depends(require_permission(Permission.MANAGE_TENANTS)),
):
    async with AsyncSessionLocal() as db:
        try:
            await tenant_svc.rotate_tenant_bot_token(
                db=db, tenant_id=tenant_id,
                new_bot_token=req.new_bot_token,
                actor_id=admin["sub"],
            )
            await db.commit()
            # Stop the running instance — caller must re-activate
            await tenant_bot_manager.stop_tenant(tenant_id)
            return {"ok": True, "message": "Token rotated. Re-activate the tenant to restart the bot."}
        except (TenantNotFoundError, ValidationError) as e:
            raise HTTPException(status_code=422, detail=e.user_message)


@router.get("/tenants/{tenant_id}/stats")
async def get_tenant_stats(
    tenant_id: int,
    admin=Depends(require_permission(Permission.MANAGE_TENANTS)),
):
    async with AsyncSessionLocal() as db:
        try:
            stats = await tenant_svc.get_tenant_stats(db, tenant_id)
            # Add runtime stats from the bot manager
            inst = tenant_bot_manager.get_instance(tenant_id)
            if inst:
                stats["bot_healthy"]    = inst.is_healthy
                stats["update_count"]   = inst.update_count
                stats["started_at"]     = inst.started_at.isoformat() if inst.started_at else None
                stats["last_update_at"] = inst.last_update_at.isoformat() if inst.last_update_at else None
            else:
                stats["bot_healthy"] = False
                stats["update_count"] = 0
            return stats
        except TenantNotFoundError as e:
            raise HTTPException(status_code=404, detail=e.user_message)


# ── Webhook receiver ───────────────────────────────────────────────────────────

@bot_webhook.post("/bots/{slug}/webhook")
async def receive_tenant_webhook(
    slug: str,
    request: Request,
    x_telegram_bot_api_secret_token: str = Header(default=""),
):
    """
    Receive Telegram updates for a tenant bot.

    Telegram sends the webhook_secret we registered as X-Telegram-Bot-Api-Secret-Token.
    We verify it matches the tenant's webhook_secret before processing.
    """
    # Look up the instance by slug
    inst = tenant_bot_manager.get_instance_by_slug(slug)
    if inst is None:
        # Tenant not running — return 200 so Telegram doesn't retry
        logger.warning("tenant_webhook_no_instance", slug=slug)
        return Response(status_code=200)

    # Verify the secret token
    import hmac
    if not hmac.compare_digest(
        x_telegram_bot_api_secret_token,
        inst._webhook_secret,
    ):
        logger.warning("tenant_webhook_bad_secret", slug=slug)
        return Response(status_code=403)

    # Process the update
    try:
        body = await request.json()
        await inst.process_update(body)
    except Exception as exc:
        logger.error(
            "tenant_webhook_error",
            slug=slug,
            tenant_id=inst.tenant_id,
            error=str(exc),
        )
        # Return 200 so Telegram doesn't retry — we log the error
    return Response(status_code=200)


# ── Helper ─────────────────────────────────────────────────────────────────────

def _to_out(tenant) -> dict:
    """Convert a Tenant ORM object to a safe response dict."""
    return {
        "id":            tenant.id,
        "slug":          tenant.slug,
        "display_name":  tenant.display_name,
        "plan":          tenant.plan,
        "is_active":     tenant.is_active,
        "bot_username":  getattr(tenant, "bot_username", None),
        "owner_user_id": tenant.owner_user_id,
        "created_at":    tenant.created_at.isoformat() if tenant.created_at else None,
        # bot_token_enc and webhook_secret NEVER included
    }
