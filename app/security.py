"""HMAC initData validation, Fernet wrapping of provider keys, idempotency helpers.

Never log secret values. Never send provider keys or the bot token to Telegram.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import time
import urllib.parse
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
import base64

from app.config import Settings, get_settings

log = logging.getLogger("falaron.security")

INIT_DATA_MAX_AGE_SECONDS = 24 * 60 * 60
IDEMPOTENCY_TTL_SECONDS = 7 * 24 * 60 * 60


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_public_id(prefix: str, hex_chars: int = 8) -> str:
    n_bytes = max(2, (int(hex_chars) + 1) // 2)
    token = secrets.token_hex(n_bytes).upper()[:hex_chars]
    return f"{prefix}-{token}"


def new_idempotency_key(*parts: str) -> str:
    raw = "|".join(parts) if parts else uuid4().hex
    return sha256(raw.encode("utf-8")).hexdigest()


def _fernet_from_secret(secret: str) -> Fernet:
    digest = sha256(secret.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _derive_local_secret(settings: Settings) -> str:
    """Deterministic local key when SECRET_KEY is unset. Solo/dev only."""
    token = settings.bot_token_value()
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"falaron-smm-local-v1",
        iterations=120_000,
    )
    raw = kdf.derive(token.encode("utf-8"))
    return base64.urlsafe_b64encode(raw).decode("ascii")


def get_fernet(settings: Settings | None = None) -> Fernet:
    settings = settings or get_settings()
    if settings.secret_key:
        value = settings.secret_key.get_secret_value().strip()
        if value:
            if len(value) == 44 and value.endswith("="):
                try:
                    return Fernet(value.encode("ascii"))
                except (ValueError, InvalidToken):
                    pass
            return _fernet_from_secret(value)
    if settings.is_production:
        # A key derived from the bot token would change if the token is rotated and silently
        # make every stored provider credential undecryptable. Refuse instead of guessing.
        raise RuntimeError("SECRET_KEY is required in production")
    return _fernet_from_secret(_derive_local_secret(settings))


def encrypt_secret(plaintext: str, settings: Settings | None = None) -> str:
    return get_fernet(settings).encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_secret(token: str, settings: Settings | None = None) -> str:
    try:
        return get_fernet(settings).decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError("Unable to decrypt stored credential") from exc


def mask_secret(value: str | None, keep: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= keep:
        return "*" * len(value)
    return value[:keep] + "…" + "*" * min(8, len(value) - keep)


def validate_telegram_init_data(
    init_data: str,
    bot_token: str,
    max_age_seconds: int = INIT_DATA_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    """Validate Telegram WebApp initData per the official HMAC-SHA256 spec.

    Returns the parsed fields (including nested `user` dict). Raises ValueError
    on any failure. The caller must treat the client payload as untrusted
    besides the identity proven here.
    """
    if not init_data or not isinstance(init_data, str):
        raise ValueError("initData is required")

    parsed = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True, strict_parsing=False))
    received_hash = parsed.pop("hash", None)
    if not received_hash:
        raise ValueError("initData hash missing")

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    computed = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(computed, received_hash):
        raise ValueError("initData HMAC mismatch")

    auth_date_raw = parsed.get("auth_date")
    if not auth_date_raw:
        raise ValueError("initData auth_date missing")
    try:
        auth_date = int(auth_date_raw)
    except ValueError as exc:
        raise ValueError("initData auth_date invalid") from exc
    age = time.time() - auth_date
    if age > max_age_seconds or age < -300:
        raise ValueError("initData expired")

    user_raw = parsed.get("user")
    if user_raw:
        try:
            parsed["user"] = json.loads(user_raw)
        except json.JSONDecodeError as exc:
            raise ValueError("initData user payload invalid") from exc
        if not isinstance(parsed["user"], dict) or "id" not in parsed["user"]:
            raise ValueError("initData user id missing")
    return parsed


def extract_telegram_user_id(init_data_fields: dict[str, Any]) -> int:
    user = init_data_fields.get("user") or {}
    try:
        return int(user["id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Telegram user id missing from initData") from exc
