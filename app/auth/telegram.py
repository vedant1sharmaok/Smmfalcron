"""
Telegram initData verification.

The initData string is sent by the Telegram client when opening a Mini App.
It contains user data and a HMAC-SHA256 signature.

Verification algorithm (per Telegram docs):
  1. Parse key=value pairs from initData query string
  2. Extract and remove 'hash' field
  3. Sort remaining pairs alphabetically by key
  4. Join with '\n' → data_check_string
  5. secret_key = HMAC-SHA256(key="WebAppData", msg=bot_token)
  6. expected   = HMAC-SHA256(key=secret_key, msg=data_check_string).hexdigest()
  7. Compare expected with extracted hash (constant-time)
  8. Verify auth_date is within max_age seconds of now
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import unquote, parse_qsl

from app.core.exceptions import InvalidInitDataError
from app.core.logging import get_logger

logger = get_logger(__name__)


def verify_init_data(init_data: str) -> dict:
    """
    Verify Telegram initData and return the parsed user dict.

    Raises InvalidInitDataError on:
      - Missing hash field
      - Signature mismatch
      - Stale auth_date (> settings.telegram_init_data_max_age seconds)

    Returns dict with keys: id, first_name, last_name, username, language_code, etc.
    """
    from app.core.config import settings

    if not init_data:
        raise InvalidInitDataError(detail="Empty initData")

    # Parse the query string
    params = dict(parse_qsl(init_data, keep_blank_values=True))

    received_hash = params.pop("hash", None)
    if not received_hash:
        raise InvalidInitDataError(detail="Missing hash in initData")

    # Build data_check_string
    data_check_string = "\n".join(
        f"{k}={v}" for k, v in sorted(params.items())
    )

    # Derive secret key
    secret_key = hmac.new(
        b"WebAppData",
        settings.bot_token.encode(),
        hashlib.sha256,
    ).digest()

    # Compute expected hash
    expected = hmac.new(
        secret_key,
        data_check_string.encode(),
        hashlib.sha256,
    ).hexdigest()

    # Constant-time compare
    if not hmac.compare_digest(expected, received_hash):
        raise InvalidInitDataError(
            detail="initData HMAC verification failed",
        )

    # Check staleness
    auth_date_str = params.get("auth_date", "0")
    try:
        auth_date = int(auth_date_str)
    except ValueError:
        raise InvalidInitDataError(detail=f"Invalid auth_date: {auth_date_str!r}")

    age = int(time.time()) - auth_date
    if age > settings.telegram_init_data_max_age:
        raise InvalidInitDataError(
            detail=f"initData is stale: age={age}s max={settings.telegram_init_data_max_age}s",
        )

    # Parse user JSON
    user_json = params.get("user", "{}")
    try:
        user = json.loads(unquote(user_json))
    except Exception:
        raise InvalidInitDataError(detail="Failed to parse user JSON in initData")

    if not user.get("id"):
        raise InvalidInitDataError(detail="Missing user.id in initData")

    logger.debug("telegram_init_data_verified", user_id=user.get("id"))
    return user
