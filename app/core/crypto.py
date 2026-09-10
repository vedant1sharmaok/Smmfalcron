"""
Cryptographic primitives for the SMM platform.

AES-256-GCM encryption:
  - Separate context AAD per data type prevents key-swap attacks
  - 12-byte random nonce per encryption call
  - Output: nonce (12 bytes) + ciphertext + tag (16 bytes)

Contexts and their AAD:
  smm:provider:credential:v1  — provider API keys
  smm:bot:token:v1            — Telegram bot tokens
  smm:config:v1               — generic encrypted config values

API keys:
  Format: smm_{48 hex chars} = 52 chars total
  Storage: SHA-256(raw_key) stored in DB — raw never persisted
  Prefix check: if not raw_key.startswith("smm_") → reject before DB
"""

from __future__ import annotations

import hashlib
import os
import secrets
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


# ── Key loading ────────────────────────────────────────────────────────────────

def _get_key(hex_key: str, context: str) -> bytes:
    if not hex_key or len(hex_key) != 64:
        raise RuntimeError(
            f"Encryption key for context {context!r} must be 64 hex chars (32 bytes). "
            f"Got length={len(hex_key) if hex_key else 0}"
        )
    return bytes.fromhex(hex_key)


# ── AES-256-GCM ────────────────────────────────────────────────────────────────

def _encrypt(plaintext: str, key: bytes, aad: bytes) -> bytes:
    """Encrypt plaintext string → nonce + ciphertext + tag."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = secrets.token_bytes(12)
    ct    = AESGCM(key).encrypt(nonce, plaintext.encode(), aad)
    return nonce + ct


def _decrypt(ciphertext: bytes, key: bytes, aad: bytes) -> str:
    """Decrypt nonce + ciphertext + tag → plaintext string."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = ciphertext[:12]
    ct    = ciphertext[12:]
    return AESGCM(key).decrypt(nonce, ct, aad).decode()


# ── Provider credential (API key) ──────────────────────────────────────────────

_PROVIDER_AAD = b"smm:provider:credential:v1"


def encrypt_provider_credential(plaintext: str) -> bytes:
    """Encrypt a provider API key or secret."""
    from app.core.config import settings
    key = _get_key(settings.encryption_key, "encryption_key")
    return _encrypt(plaintext, key, _PROVIDER_AAD)


def decrypt_provider_credential(ciphertext: bytes) -> str:
    """Decrypt a provider API key or secret."""
    from app.core.config import settings
    key = _get_key(settings.encryption_key, "encryption_key")
    return _decrypt(ciphertext, key, _PROVIDER_AAD)


# ── Bot token ─────────────────────────────────────────────────────────────────

_BOT_TOKEN_AAD = b"smm:bot:token:v1"


def encrypt_bot_token(token: str) -> bytes:
    """Encrypt a Telegram bot token."""
    from app.core.config import settings
    key = _get_key(settings.encryption_key, "encryption_key")
    return _encrypt(token, key, _BOT_TOKEN_AAD)


def decrypt_bot_token(ciphertext: bytes) -> str:
    """Decrypt a Telegram bot token."""
    from app.core.config import settings
    key = _get_key(settings.encryption_key, "encryption_key")
    return _decrypt(ciphertext, key, _BOT_TOKEN_AAD)


# ── Config values ─────────────────────────────────────────────────────────────

_CONFIG_AAD = b"smm:config:v1"


def encrypt_config_value(value: str) -> bytes:
    """Encrypt a generic config value (e.g. webhook secret)."""
    from app.core.config import settings
    key = _get_key(settings.config_encryption_key, "config_encryption_key")
    return _encrypt(value, key, _CONFIG_AAD)


def decrypt_config_value(ciphertext: bytes) -> str:
    """Decrypt a generic config value."""
    from app.core.config import settings
    key = _get_key(settings.config_encryption_key, "config_encryption_key")
    return _decrypt(ciphertext, key, _CONFIG_AAD)


# ── API key generation ─────────────────────────────────────────────────────────

_API_KEY_PREFIX  = "smm_"
_API_KEY_ENTROPY = 48    # hex chars after prefix → 52 total chars


def generate_api_key() -> tuple[str, str]:
    """
    Generate a new API key.

    Returns (raw_key, hashed_key):
      raw_key    — smm_{48 hex chars} — shown to user ONCE, never stored
      hashed_key — SHA-256(raw_key) hex — stored in DB for lookup
    """
    entropy = secrets.token_hex(_API_KEY_ENTROPY // 2)
    raw     = f"{_API_KEY_PREFIX}{entropy}"
    hashed  = hash_api_key(raw)
    return raw, hashed


def hash_api_key(raw_key: str) -> str:
    """SHA-256 hash of an API key. Deterministic — used for DB lookup."""
    return hashlib.sha256(raw_key.encode()).hexdigest()
