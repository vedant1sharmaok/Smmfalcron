"""Append-only audit trail for sensitive actions."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog
from app.security import utcnow


def _short(value: Any, limit: int = 2000) -> str | None:
    if value is None:
        return None
    text = value if isinstance(value, str) else json.dumps(value, default=str, separators=(",", ":"))
    return text[:limit]


async def audit(
    session: AsyncSession,
    *,
    actor: str | int,
    action: str,
    target: str | None = None,
    old: Any = None,
    new: Any = None,
    reason: str | None = None,
) -> AuditLog:
    """Record who did what. Never pass secrets in old/new."""
    row = AuditLog(
        actor=str(actor)[:48],
        action=action[:64],
        target=(target[:80] if target else None),
        old_value=_short(old),
        new_value=_short(new),
        reason=(reason[:255] if reason else None),
        created_at=utcnow(),
    )
    session.add(row)
    await session.flush()
    return row
