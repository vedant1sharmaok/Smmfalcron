"""
Redis session management.

Session types and TTLs:
  bot     — 30 days (Telegram bot interactions)
  miniapp — 24 hours (Telegram Mini App)
  api     — 90 days (Own SMM API, refreshed on use)
  admin   — managed by JWT TTL (not stored in Redis)

Session token format: 64-char hex string
Session key pattern:  session:{token}
User sessions set:    user_sessions:{user_id}  (SMEMBERS for revoke-all)
"""
from __future__ import annotations

import secrets
from datetime import timedelta
import json

from app.core.logging import get_logger
from app.core.redis import get_redis

logger = get_logger(__name__)

_TTL: dict[str, int] = {
    "bot":     int(timedelta(days=30).total_seconds()),
    "miniapp": int(timedelta(hours=24).total_seconds()),
    "api":     int(timedelta(days=90).total_seconds()),
}

_SESSION_KEY      = "session:{token}"
_USER_SESSIONS    = "user_sessions:{user_id}"


def _skey(token: str)   -> str: return f"session:{token}"
def _uskey(user_id: int) -> str: return f"user_sessions:{user_id}"


async def create_session(
    user_id: int,
    session_type: str,
    extra: dict | None = None,
) -> str:
    """
    Create a new Redis session. Returns the session token.
    Also registers the token in the user's session set.
    """
    token   = secrets.token_hex(32)
    ttl     = _TTL.get(session_type, _TTL["miniapp"])
    payload = {"user_id": user_id, "type": session_type, **(extra or {})}

    r = get_redis()
    pipe = r.pipeline()
    pipe.set(_skey(token), json.dumps(payload), ex=ttl)
    pipe.sadd(_uskey(user_id), token)
    pipe.expire(_uskey(user_id), ttl)
    await pipe.execute()

    logger.debug("session_created", user_id=user_id, session_type=session_type)
    return token


async def get_session(token: str) -> dict | None:
    """Fetch session data. Returns None if expired or missing."""
    r = get_redis()
    raw = await r.get(_skey(token))
    if not raw:
        return None
    return json.loads(raw)


async def revoke_session(token: str, user_id: int | None = None) -> None:
    """Revoke a single session token."""
    r = get_redis()
    pipe = r.pipeline()
    pipe.delete(_skey(token))
    if user_id:
        pipe.srem(_uskey(user_id), token)
    await pipe.execute()


async def revoke_all_sessions(user_id: int) -> int:
    """Revoke all active sessions for a user. Returns count revoked."""
    r    = get_redis()
    tokens = await r.smembers(_uskey(user_id))
    if not tokens:
        return 0

    pipe = r.pipeline()
    for token in tokens:
        pipe.delete(_skey(token.decode() if isinstance(token, bytes) else token))
    pipe.delete(_uskey(user_id))
    await pipe.execute()

    logger.info("all_sessions_revoked", user_id=user_id, count=len(tokens))
    return len(tokens)


async def refresh_session(token: str, session_type: str) -> bool:
    """
    Extend session TTL (called on each API request for api-type sessions).
    Returns False if session no longer exists.
    """
    ttl = _TTL.get(session_type, _TTL["miniapp"])
    r   = get_redis()
    ok  = await r.expire(_skey(token), ttl)
    return bool(ok)
