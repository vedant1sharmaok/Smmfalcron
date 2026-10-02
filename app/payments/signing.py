"""Webhook signature helpers (stdlib only).

A payment webhook is only trusted after its HMAC signature over the *raw* request
body has been verified in constant time. Never verify against re-serialised JSON.
"""

from __future__ import annotations

import hashlib
import hmac


def hmac_sha256_hex(secret: str | bytes, body: bytes) -> str:
    key = secret.encode("utf-8") if isinstance(secret, str) else secret
    return hmac.new(key, body, hashlib.sha256).hexdigest()


def verify_hmac_sha256_hex(secret: str | bytes | None, body: bytes, provided: str | None) -> bool:
    """Constant-time check of a hex HMAC-SHA256 signature. False on any missing input."""
    if not secret or not provided or not isinstance(body, (bytes, bytearray)):
        return False
    expected = hmac_sha256_hex(secret, bytes(body))
    return hmac.compare_digest(expected, provided.strip().lower())
