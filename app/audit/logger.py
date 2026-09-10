"""
Append-only audit logger.

Writes structured audit events to the audit_log table.
Rules:
  - db.delete is NEVER called on audit_log — append only
  - credentials_enc, api_key, bot_token_enc are NEVER logged
  - All other sensitive fields are scrubbed before writing

Usage:
  from app.audit.logger import audit_log

  await audit_log(
      db=db,
      actor_id=42,
      action="order.create",
      resource="order",
      resource_id=order.id,
      details={"quantity": 1000, "service_id": 5},
  )
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.core.logging import get_logger
from app.core.models import AuditLog

logger = get_logger(__name__)

# Fields that must never appear in the audit log
_SCRUB_FIELDS = frozenset({
    "credentials_enc", "api_key", "api_key_hash", "bot_token_enc",
    "bot_token", "webhook_secret", "password", "password_hash",
    "totp_secret", "encryption_key", "secret",
})


def _scrub(details: dict) -> dict:
    """Remove sensitive keys from details dict before writing to DB."""
    if not details:
        return {}
    return {
        k: "***REDACTED***" if k in _SCRUB_FIELDS else v
        for k, v in details.items()
    }


async def audit_log(
    db,
    action: str,
    resource: str,
    actor_id: int | None = None,
    resource_id: str | None = None,
    details: dict[str, Any] | None = None,
    ip_hash: str | None = None,
    tenant_id: int | None = None,
) -> None:
    """
    Write one audit log entry. Never raises — audit failures are logged
    but do not propagate to the caller.

    Append-only: this function never calls db.delete or db.update
    on the audit_log table.
    """
    try:
        entry = AuditLog(
            actor_id=actor_id,
            action=action,
            resource=resource,
            resource_id=str(resource_id) if resource_id is not None else None,
            details=_scrub(details or {}),
            ip_hash=ip_hash,
            tenant_id=tenant_id,
            created_at=datetime.now(timezone.utc),
        )
        db.add(entry)
        await db.flush()

        logger.debug(
            "audit_event",
            action=action,
            resource=resource,
            actor_id=actor_id,
            resource_id=resource_id,
        )
    except Exception as exc:
        # Never let audit failures break the main operation
        logger.error("audit_log_write_failed", action=action, error=str(exc))
