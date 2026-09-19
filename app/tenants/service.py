"""
Hosted bots multi-tenancy — service layer.

A Tenant is an independent customer running their own white-label SMM bot.
Each tenant has:
  - Their own Telegram bot token (AES-256-GCM encrypted at rest)
  - Their own webhook secret
  - Their own bot username and display name
  - A reference to their owner user (the admin who set it up)
  - Per-tenant rate limits, pricing overrides, and feature flags
  - Isolated audit trail (tenant_id on every audit log entry)

Architecture:
  The platform runs one bot instance per active tenant.
  Each tenant bot shares the same order engine, wallet ledger, and provider
  registry — isolation is at the data layer (tenant_id on every row that
  needs it: users, wallets, orders, payments, audit_log).

  The hosted bot manager (TenantBotManager) maintains a registry of running
  bot instances and handles startup/shutdown.

Tenant lifecycle:
  1. Admin creates tenant (POST /admin/tenants)
  2. Bot token is encrypted and stored
  3. TenantBotManager starts the bot instance (webhook or polling)
  4. Bot is live — users interact via their tenant's bot username
  5. Admin can suspend/activate/delete tenants

Security:
  - Bot tokens are never returned in any API response after creation
  - Each tenant has a separate webhook secret
  - Tenant-scoped users cannot access another tenant's data
  - The admin who creates a tenant is the only admin who can delete it
    (unless the platform superadmin acts)
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select, update, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import (
    decrypt_provider_credential,
    encrypt_provider_credential,
    generate_api_key,
)
from app.core.exceptions import (
    TenantError,
    TenantNotFoundError,
    ValidationError,
)
from app.core.logging import get_logger
from app.core.models import Tenant, TenantUser

logger = get_logger(__name__)

_MAX_TENANTS_PER_OWNER = 5


# ── CRUD ───────────────────────────────────────────────────────────────────────

async def create_tenant(
    db: AsyncSession,
    owner_user_id: int,
    bot_token: str,
    display_name: str,
    slug: str,
    plan: str = "basic",
    actor_id: int | None = None,
) -> Tenant:
    """
    Create a new hosted bot tenant.

    bot_token is encrypted immediately — never stored plaintext.
    A unique webhook_secret is generated automatically.

    Raises:
      ValidationError — slug already taken, bot_token invalid format,
                        or owner has reached tenant limit
    """
    import re

    # Validate bot token format: {digits}:{35-chars}
    if not re.match(r'^\d{8,12}:[A-Za-z0-9_-]{35}$', bot_token):
        raise ValidationError(
            detail=f"Invalid bot token format",
            user_message="Bot token format is invalid. Expected: <id>:<token>",
        )

    # Validate slug: 3-32 chars, lowercase alphanumeric + hyphens
    if not re.match(r'^[a-z0-9][a-z0-9-]{1,30}[a-z0-9]$', slug):
        raise ValidationError(
            detail=f"Invalid tenant slug: {slug!r}",
            user_message="Slug must be 3-32 lowercase alphanumeric characters or hyphens.",
        )

    # Check slug uniqueness
    existing_slug = (await db.execute(
        select(Tenant).where(Tenant.slug == slug)
    )).scalar_one_or_none()
    if existing_slug is not None:
        raise ValidationError(
            detail=f"Tenant slug {slug!r} already in use",
            user_message=f"The slug '{slug}' is already taken.",
        )

    # Check owner tenant limit
    owner_count = (await db.execute(
        select(func.count()).where(
            Tenant.owner_user_id == owner_user_id,
            Tenant.is_deleted == False,
        )
    )).scalar_one()
    if owner_count >= _MAX_TENANTS_PER_OWNER:
        raise ValidationError(
            detail=f"Owner {owner_user_id} has reached max tenant limit ({_MAX_TENANTS_PER_OWNER})",
            user_message=f"Maximum of {_MAX_TENANTS_PER_OWNER} bots per account.",
        )

    # Encrypt the bot token and generate webhook secret
    encrypted_token  = encrypt_provider_credential(bot_token)
    webhook_secret   = secrets.token_hex(32)

    tenant = Tenant(
        owner_user_id=owner_user_id,
        slug=slug,
        display_name=display_name.strip(),
        bot_token_enc=encrypted_token,
        webhook_secret=webhook_secret,
        plan=plan,
        is_active=False,   # not active until bot is verified
        is_deleted=False,
        config={},
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    db.add(tenant)
    await db.flush()

    logger.info(
        "tenant_created",
        tenant_id=tenant.id,
        slug=slug,
        owner_user_id=owner_user_id,
        actor_id=actor_id,
    )
    return tenant


async def get_tenant(db: AsyncSession, tenant_id: int) -> Tenant:
    """Fetch a tenant by ID. Raises TenantNotFoundError if not found or deleted."""
    tenant = await db.get(Tenant, tenant_id)
    if tenant is None or tenant.is_deleted:
        raise TenantNotFoundError(
            detail=f"Tenant {tenant_id} not found",
            user_message="Tenant not found.",
        )
    return tenant


async def get_tenant_by_slug(db: AsyncSession, slug: str) -> Tenant:
    """Fetch a tenant by slug. Raises TenantNotFoundError if not found."""
    tenant = (await db.execute(
        select(Tenant).where(Tenant.slug == slug, Tenant.is_deleted == False)
    )).scalar_one_or_none()
    if tenant is None:
        raise TenantNotFoundError(
            detail=f"Tenant with slug {slug!r} not found",
            user_message=f"Bot '{slug}' not found.",
        )
    return tenant


async def list_tenants(
    db: AsyncSession,
    owner_user_id: int | None = None,
    is_active: bool | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[Tenant], int]:
    """List tenants with optional filters. Returns (items, total)."""
    stmt = select(Tenant).where(Tenant.is_deleted == False)
    if owner_user_id is not None:
        stmt = stmt.where(Tenant.owner_user_id == owner_user_id)
    if is_active is not None:
        stmt = stmt.where(Tenant.is_active == is_active)

    total = (await db.execute(
        select(func.count()).select_from(stmt.subquery())
    )).scalar_one()

    items = list((await db.execute(
        stmt.offset((page - 1) * page_size).limit(page_size)
        .order_by(Tenant.created_at.desc())
    )).scalars().all())

    return items, total


async def update_tenant(
    db: AsyncSession,
    tenant_id: int,
    updates: dict[str, Any],
    actor_id: int,
) -> Tenant:
    """
    Update mutable tenant fields.
    Allowed fields: display_name, plan, config, is_active.
    bot_token and slug are immutable after creation.
    """
    _ALLOWED = {"display_name", "plan", "config", "is_active"}
    invalid = set(updates.keys()) - _ALLOWED
    if invalid:
        raise ValidationError(
            detail=f"Cannot update fields: {invalid}",
            user_message=f"Fields {invalid} cannot be modified.",
        )

    tenant = await get_tenant(db, tenant_id)
    for field, value in updates.items():
        setattr(tenant, field, value)
    tenant.updated_at = datetime.now(timezone.utc)
    await db.flush()

    logger.info("tenant_updated", tenant_id=tenant_id, fields=list(updates), actor_id=actor_id)
    return tenant


async def rotate_tenant_bot_token(
    db: AsyncSession,
    tenant_id: int,
    new_bot_token: str,
    actor_id: int,
) -> None:
    """
    Rotate a tenant's bot token.
    The tenant bot must be restarted after rotation for the new token to take effect.
    """
    import re
    if not re.match(r'^\d{8,12}:[A-Za-z0-9_-]{35}$', new_bot_token):
        raise ValidationError(
            detail="Invalid bot token format",
            user_message="Bot token format is invalid.",
        )

    tenant = await get_tenant(db, tenant_id)
    tenant.bot_token_enc = encrypt_provider_credential(new_bot_token)
    tenant.updated_at    = datetime.now(timezone.utc)
    await db.flush()

    logger.info("tenant_bot_token_rotated", tenant_id=tenant_id, actor_id=actor_id)


async def activate_tenant(
    db: AsyncSession,
    tenant_id: int,
    actor_id: int,
) -> Tenant:
    """
    Activate a tenant. Verifies the bot token is valid before activating.
    The TenantBotManager must be notified separately to start the bot instance.
    """
    tenant = await get_tenant(db, tenant_id)
    if tenant.is_active:
        return tenant

    # Verify the bot token resolves to a real Telegram bot
    bot_token = decrypt_provider_credential(tenant.bot_token_enc)
    bot_info  = await _verify_bot_token(bot_token)
    if not bot_info:
        raise TenantError(
            detail=f"Bot token for tenant {tenant_id} failed Telegram verification",
            user_message="Bot token is invalid or the bot does not exist.",
        )

    tenant.is_active    = True
    tenant.bot_username = bot_info.get("username", "")
    tenant.updated_at   = datetime.now(timezone.utc)
    await db.flush()

    logger.info(
        "tenant_activated",
        tenant_id=tenant_id,
        bot_username=tenant.bot_username,
        actor_id=actor_id,
    )
    return tenant


async def suspend_tenant(
    db: AsyncSession,
    tenant_id: int,
    reason: str,
    actor_id: int,
) -> None:
    """
    Suspend a tenant (set is_active=False).
    Running bot instance must be stopped separately via TenantBotManager.
    """
    tenant = await get_tenant(db, tenant_id)
    tenant.is_active  = False
    tenant.updated_at = datetime.now(timezone.utc)
    await db.flush()

    logger.info(
        "tenant_suspended",
        tenant_id=tenant_id,
        reason=reason,
        actor_id=actor_id,
    )


async def delete_tenant(
    db: AsyncSession,
    tenant_id: int,
    actor_id: int,
) -> None:
    """
    Soft-delete a tenant.
    The bot token is zeroed out (cannot be recovered).
    All tenant users are deactivated.
    """
    tenant = await get_tenant(db, tenant_id)

    # Zero out the bot token — cannot be recovered after deletion
    tenant.bot_token_enc = b""
    tenant.is_active     = False
    tenant.is_deleted    = True
    tenant.updated_at    = datetime.now(timezone.utc)

    # Deactivate all tenant users
    await db.execute(
        update(TenantUser)
        .where(TenantUser.tenant_id == tenant_id)
        .values(is_active=False)
    )
    await db.flush()

    logger.info("tenant_deleted", tenant_id=tenant_id, actor_id=actor_id)


# ── Tenant user management ────────────────────────────────────────────────────

async def get_or_create_tenant_user(
    db: AsyncSession,
    tenant_id: int,
    telegram_id: int,
    first_name: str,
    username: str | None = None,
) -> TenantUser:
    """
    Get or create a TenantUser record for a Telegram user interacting
    with a specific tenant bot.

    Each (tenant_id, telegram_id) pair is unique — one user can interact
    with multiple tenant bots but has a separate profile for each.
    """
    existing = (await db.execute(
        select(TenantUser).where(
            TenantUser.tenant_id == tenant_id,
            TenantUser.telegram_id == telegram_id,
        )
    )).scalar_one_or_none()

    if existing is not None:
        # Update name fields if changed
        if existing.first_name != first_name or existing.username != username:
            existing.first_name = first_name
            existing.username   = username
            await db.flush()
        return existing

    tenant_user = TenantUser(
        tenant_id=tenant_id,
        telegram_id=telegram_id,
        first_name=first_name,
        username=username,
        is_active=True,
        is_banned=False,
        wallet_balance=Decimal("0"),
        created_at=datetime.now(timezone.utc),
    )
    db.add(tenant_user)
    await db.flush()

    logger.info(
        "tenant_user_created",
        tenant_id=tenant_id,
        telegram_id=telegram_id,
    )
    return tenant_user


async def get_tenant_stats(
    db: AsyncSession,
    tenant_id: int,
) -> dict[str, Any]:
    """
    Return usage statistics for a tenant.
    Used by the admin panel's tenant detail view.
    """
    from app.core.models import Order, Payment
    from sqlalchemy import text

    # Total users
    user_count = (await db.execute(
        select(func.count()).where(
            TenantUser.tenant_id == tenant_id,
            TenantUser.is_active == True,
        )
    )).scalar_one()

    # Total orders (scoped to tenant)
    order_count = (await db.execute(
        select(func.count()).where(Order.tenant_id == tenant_id)
    )).scalar_one()

    # Total revenue (sum of completed order amounts)
    revenue = (await db.execute(
        select(func.sum(Order.price_charged)).where(
            Order.tenant_id == tenant_id,
            Order.status.in_(["completed", "partial"]),
        )
    )).scalar_one() or Decimal("0")

    # Active users last 30 days
    from datetime import timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    active_30d = (await db.execute(
        select(func.count()).where(
            TenantUser.tenant_id == tenant_id,
            TenantUser.last_activity_at >= cutoff,
        )
    )).scalar_one()

    return {
        "tenant_id":   tenant_id,
        "user_count":  user_count,
        "order_count": order_count,
        "revenue_inr": str(revenue.quantize(Decimal("0.01"))),
        "active_30d":  active_30d,
    }


# ── Internal helpers ───────────────────────────────────────────────────────────

async def _verify_bot_token(bot_token: str) -> dict | None:
    """
    Verify a bot token by calling the Telegram getMe API.
    Returns the bot info dict on success, None on failure.
    """
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"https://api.telegram.org/bot{bot_token}/getMe"
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
            if not data.get("ok"):
                return None
            return data.get("result", {})
    except Exception as exc:
        logger.warning("tenant_bot_token_verify_error", error=str(exc))
        return None


def get_decrypted_bot_token(tenant: Tenant) -> str:
    """
    Decrypt and return the bot token for a tenant.
    Only called by the bot manager — never in API response paths.
    """
    if not tenant.bot_token_enc:
        raise TenantError(
            detail=f"Tenant {tenant.id} has no bot token",
            user_message="Bot token not configured.",
        )
    return decrypt_provider_credential(tenant.bot_token_enc)
