"""
CMS — Phase 18.

Manages content blocks displayed in the Telegram bot and Mini App:
  - Welcome messages (per-locale, with template variables)
  - Service category descriptions
  - Banner/announcement messages for broadcasts
  - Terms of service and policy text
  - Custom menu button labels

ContentBlock schema:
  key         — unique identifier, e.g. "welcome_message", "tos_text"
  locale      — "en", "ru", "hi", "ar" (default "en")
  content     — the template string with {variable} placeholders
  is_active   — whether this block is served
  version     — incremented on each update (audit trail)
  updated_by  — admin user_id who last updated it
  updated_at  — timestamp

Template variables (safe subset — server-resolved before delivery):
  {first_name}    — user's first name
  {balance}       — formatted wallet balance (₹123.45)
  {order_count}   — user's total order count
  {plan}          — user's plan name (basic/premium/vip)
  {bot_name}      — the bot's display name
  {support_link}  — configured support link

Broadcast:
  create_broadcast() queues a message to all active users (or a segment).
  The broadcast worker dequeues and sends with rate limiting (30/sec Telegram limit).
  Broadcast records track delivery stats (sent, failed, pending).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import CMSError, ValidationError
from app.core.logging import get_logger
from app.core.models import ContentBlock, Broadcast

logger = get_logger(__name__)

# ── Safe template variables ────────────────────────────────────────────────────

_SAFE_VARS = frozenset({
    "first_name", "balance", "order_count", "plan", "bot_name", "support_link"
})

_VAR_PATTERN = re.compile(r'\{([^}]+)\}')


def _validate_template(content: str) -> list[str]:
    """
    Validate template variables in a content string.
    Returns list of invalid variable names (empty if all valid).
    """
    found    = set(_VAR_PATTERN.findall(content))
    invalid  = found - _SAFE_VARS
    return sorted(invalid)


def render_template(content: str, variables: dict[str, str]) -> str:
    """
    Render a content template with the provided variable values.
    Unknown variables are left as-is (not removed) so they're visible
    if there's a misconfiguration.
    Only substitutes variables in the safe allowlist.
    """
    safe_vars = {k: v for k, v in variables.items() if k in _SAFE_VARS}
    try:
        return content.format_map(safe_vars)
    except (KeyError, ValueError):
        # Partial substitution — replace what we can, leave the rest
        result = content
        for key, value in safe_vars.items():
            result = result.replace(f"{{{key}}}", value)
        return result


# ── Content block CRUD ────────────────────────────────────────────────────────

async def get_content_block(
    db: AsyncSession,
    key: str,
    locale: str = "en",
) -> ContentBlock | None:
    """
    Fetch an active content block by key and locale.
    Falls back to 'en' if the requested locale doesn't exist.
    """
    block = (await db.execute(
        select(ContentBlock).where(
            ContentBlock.key == key,
            ContentBlock.locale == locale,
            ContentBlock.is_active == True,
        )
    )).scalar_one_or_none()

    if block is None and locale != "en":
        # Fallback to English
        block = (await db.execute(
            select(ContentBlock).where(
                ContentBlock.key == key,
                ContentBlock.locale == "en",
                ContentBlock.is_active == True,
            )
        )).scalar_one_or_none()

    return block


async def render_block(
    db: AsyncSession,
    key: str,
    variables: dict[str, str],
    locale: str = "en",
    fallback: str = "",
) -> str:
    """
    Fetch a content block and render it with variables.
    Returns fallback string if no block exists.
    """
    block = await get_content_block(db, key, locale)
    if block is None:
        return fallback
    return render_template(block.content, variables)


async def upsert_content_block(
    db: AsyncSession,
    key: str,
    content: str,
    locale: str = "en",
    actor_id: int | None = None,
) -> ContentBlock:
    """
    Create or update a content block.
    Validates template variables before saving.
    Increments version on each update.
    """
    # Validate key format: lowercase_with_underscores
    if not re.match(r'^[a-z][a-z0-9_]{1,63}$', key):
        raise ValidationError(
            detail=f"Invalid content block key: {key!r}",
            user_message="Key must be lowercase letters, digits, and underscores (2-64 chars).",
        )

    # Validate locale
    valid_locales = {"en", "ru", "hi", "ar", "es", "pt", "tr", "bn"}
    if locale not in valid_locales:
        raise ValidationError(
            detail=f"Invalid locale: {locale!r}",
            user_message=f"Locale must be one of: {', '.join(sorted(valid_locales))}.",
        )

    # Validate template variables
    invalid_vars = _validate_template(content)
    if invalid_vars:
        raise ValidationError(
            detail=f"Invalid template variables in content block: {invalid_vars}",
            user_message=(
                f"Unknown variables: {', '.join('{'+v+'}' for v in invalid_vars)}. "
                f"Allowed: {', '.join('{'+v+'}' for v in sorted(_SAFE_VARS))}."
            ),
        )

    now = datetime.now(timezone.utc)

    # Check for existing block
    existing = (await db.execute(
        select(ContentBlock).where(
            ContentBlock.key == key,
            ContentBlock.locale == locale,
        )
    )).scalar_one_or_none()

    if existing is not None:
        existing.content    = content
        existing.is_active  = True
        existing.version    = (existing.version or 0) + 1
        existing.updated_by = actor_id
        existing.updated_at = now
        await db.flush()
        logger.info("cms_block_updated", key=key, locale=locale, version=existing.version, actor_id=actor_id)
        return existing

    block = ContentBlock(
        key=key,
        locale=locale,
        content=content,
        is_active=True,
        version=1,
        updated_by=actor_id,
        created_at=now,
        updated_at=now,
    )
    db.add(block)
    await db.flush()
    logger.info("cms_block_created", key=key, locale=locale, actor_id=actor_id)
    return block


async def deactivate_content_block(
    db: AsyncSession,
    key: str,
    locale: str = "en",
    actor_id: int | None = None,
) -> None:
    """Deactivate a content block (hides it without deleting)."""
    result = await db.execute(
        update(ContentBlock)
        .where(ContentBlock.key == key, ContentBlock.locale == locale)
        .values(is_active=False, updated_by=actor_id, updated_at=datetime.now(timezone.utc))
    )
    if result.rowcount == 0:
        raise CMSError(
            detail=f"Content block {key!r}/{locale} not found",
            user_message=f"Block '{key}' ({locale}) not found.",
        )
    logger.info("cms_block_deactivated", key=key, locale=locale, actor_id=actor_id)


async def list_content_blocks(
    db: AsyncSession,
    locale: str | None = None,
    is_active: bool | None = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[ContentBlock], int]:
    """List content blocks with optional filters."""
    from sqlalchemy import func
    stmt = select(ContentBlock)
    if locale is not None:
        stmt = stmt.where(ContentBlock.locale == locale)
    if is_active is not None:
        stmt = stmt.where(ContentBlock.is_active == is_active)

    total = (await db.execute(
        select(func.count()).select_from(stmt.subquery())
    )).scalar_one()

    items = list((await db.execute(
        stmt.order_by(ContentBlock.key, ContentBlock.locale)
        .offset((page - 1) * page_size).limit(page_size)
    )).scalars().all())

    return items, total


# ── Broadcast ─────────────────────────────────────────────────────────────────

async def create_broadcast(
    db: AsyncSession,
    title: str,
    message: str,
    segment: str = "all",   # "all" | "premium" | "vip" | "inactive_30d"
    scheduled_at: datetime | None = None,
    actor_id: int | None = None,
) -> Broadcast:
    """
    Queue a broadcast message to a user segment.

    segment options:
      all          — all active, non-banned users
      premium      — users with active premium/vip
      vip          — users with active vip plan
      inactive_30d — users with no activity in last 30 days (re-engagement)

    The broadcast worker picks up undelivered broadcasts and sends
    at ≤ 30 messages/second (Telegram group limit).
    """
    if not title.strip():
        raise ValidationError(detail="Broadcast title cannot be empty")
    if not message.strip():
        raise ValidationError(detail="Broadcast message cannot be empty")

    # Validate template variables in the message
    invalid = _validate_template(message)
    if invalid:
        raise ValidationError(
            detail=f"Invalid template variables in broadcast: {invalid}",
            user_message=f"Unknown variables: {', '.join('{'+v+'}' for v in invalid)}.",
        )

    valid_segments = {"all", "premium", "vip", "inactive_30d"}
    if segment not in valid_segments:
        raise ValidationError(
            detail=f"Invalid segment: {segment!r}",
            user_message=f"Segment must be one of: {', '.join(sorted(valid_segments))}.",
        )

    now = datetime.now(timezone.utc)
    broadcast = Broadcast(
        title=title.strip(),
        message=message.strip(),
        segment=segment,
        status="pending",
        scheduled_at=scheduled_at or now,
        created_by=actor_id,
        created_at=now,
        sent_count=0,
        failed_count=0,
        pending_count=0,
    )
    db.add(broadcast)
    await db.flush()

    logger.info(
        "broadcast_created",
        broadcast_id=broadcast.id,
        title=title,
        segment=segment,
        actor_id=actor_id,
    )
    return broadcast


async def get_broadcast_stats(
    db: AsyncSession,
    broadcast_id: int,
) -> dict[str, Any]:
    """Return delivery statistics for a broadcast."""
    broadcast = await db.get(Broadcast, broadcast_id)
    if broadcast is None:
        raise CMSError(
            detail=f"Broadcast {broadcast_id} not found",
            user_message="Broadcast not found.",
        )
    total = (broadcast.sent_count or 0) + (broadcast.failed_count or 0) + (broadcast.pending_count or 0)
    return {
        "id":            broadcast.id,
        "title":         broadcast.title,
        "segment":       broadcast.segment,
        "status":        broadcast.status,
        "sent":          broadcast.sent_count or 0,
        "failed":        broadcast.failed_count or 0,
        "pending":       broadcast.pending_count or 0,
        "total":         total,
        "delivery_rate": round((broadcast.sent_count or 0) / max(total, 1) * 100, 1),
        "scheduled_at":  broadcast.scheduled_at.isoformat() if broadcast.scheduled_at else None,
        "completed_at":  broadcast.completed_at.isoformat() if getattr(broadcast, "completed_at", None) else None,
    }


# ── Well-known block keys ──────────────────────────────────────────────────────

class CmsKey:
    """Canonical content block keys — import these instead of hardcoding strings."""
    WELCOME_MESSAGE  = "welcome_message"
    HELP_TEXT        = "help_text"
    TERMS_OF_SERVICE = "terms_of_service"
    DEPOSIT_PROMPT   = "deposit_prompt"
    ORDER_CONFIRM    = "order_confirm_template"
    ORDER_COMPLETE   = "order_complete_template"
    PREMIUM_UPSELL   = "premium_upsell_message"
    MAINTENANCE_MSG  = "maintenance_message"
    BROADCAST_FOOTER = "broadcast_footer"
