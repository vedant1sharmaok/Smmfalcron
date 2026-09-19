"""
Admin credential store — Phase 12 gap fill.

Manages admin user credentials:
  - Password hashing with bcrypt (work factor 12)
  - Admin registration (first-time setup or invite-only)
  - Password verification and rotation
  - TOTP secret generation + verification (pyotp)
  - Admin session tokens (JWT, 1h TTL)
  - Partial token flow: password → partial_token → TOTP → full JWT

All admin operations are audit-logged.
Admin credentials are stored in the admins table, separate from users.

Admin table schema (add to migrations):
  CREATE TABLE admins (
    id            SERIAL PRIMARY KEY,
    username      VARCHAR(64) UNIQUE NOT NULL,
    email         VARCHAR(255) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    totp_secret   BYTEA,              -- AES-256-GCM encrypted TOTP secret
    totp_enabled  BOOLEAN DEFAULT FALSE,
    is_active     BOOLEAN DEFAULT TRUE,
    role          VARCHAR(64) DEFAULT 'admin',
    created_at    TIMESTAMPTZ DEFAULT NOW(),
    updated_at    TIMESTAMPTZ DEFAULT NOW(),
    last_login_at TIMESTAMPTZ,
    failed_attempts INTEGER DEFAULT 0,
    locked_until  TIMESTAMPTZ
  );
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.crypto import decrypt_provider_credential, encrypt_provider_credential
from app.core.exceptions import (
    AdminAuthError,
    AdminLockedError,
    InvalidCredentialsError,
    InvalidTOTPError,
    ValidationError,
)
from app.core.logging import get_logger
from app.core.models import Admin

logger = get_logger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

_BCRYPT_ROUNDS       = 12
_MAX_FAILED_ATTEMPTS = 5
_LOCKOUT_MINUTES     = 30
_PARTIAL_TOKEN_TTL_S = 300       # 5 minutes to complete TOTP step
_ADMIN_JWT_TTL_S     = 3600      # 1 hour full session

# ── Password operations ────────────────────────────────────────────────────────

def hash_password(plaintext: str) -> str:
    """
    Hash a password with bcrypt (work factor 12).
    Returns the bcrypt hash string (includes salt and work factor).
    """
    try:
        from passlib.context import CryptContext
    except ImportError:
        raise RuntimeError("passlib is required for admin password hashing")

    ctx = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=_BCRYPT_ROUNDS)
    return ctx.hash(plaintext)


def verify_password(plaintext: str, hashed: str) -> bool:
    """Verify a plaintext password against a bcrypt hash."""
    try:
        from passlib.context import CryptContext
    except ImportError:
        return False

    ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
    return ctx.verify(plaintext, hashed)


# ── TOTP operations ────────────────────────────────────────────────────────────

def generate_totp_secret() -> str:
    """
    Generate a new TOTP secret (32-char base32 string).
    Returns the plaintext secret — encrypt before storing.
    """
    try:
        import pyotp
        return pyotp.random_base32()
    except ImportError:
        # Fallback: generate 20 random bytes encoded as base32
        import base64
        return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def generate_totp_provisioning_uri(
    secret: str, username: str, issuer: str = "SMM Platform"
) -> str:
    """Return the otpauth:// URI for QR code generation."""
    try:
        import pyotp
        totp = pyotp.TOTP(secret)
        return totp.provisioning_uri(name=username, issuer_name=issuer)
    except ImportError:
        return f"otpauth://totp/{issuer}:{username}?secret={secret}&issuer={issuer}"


def verify_totp(secret: str, code: str, valid_window: int = 1) -> bool:
    """
    Verify a TOTP code against the secret.
    valid_window=1 allows one step before/after (30s tolerance).
    """
    try:
        import pyotp
        totp = pyotp.TOTP(secret)
        return totp.verify(code, valid_window=valid_window)
    except ImportError:
        return False
    except Exception:
        return False


# ── Token generation (JWT) ────────────────────────────────────────────────────

def _generate_admin_jwt(
    admin_id: int,
    username: str,
    role: str,
    partial: bool = False,
    ttl_s: int | None = None,
) -> str:
    """
    Generate a signed JWT for an admin session.
    partial=True: partial token (password verified, TOTP pending)
    partial=False: full session token
    """
    try:
        from jose import jwt
    except ImportError:
        raise RuntimeError("python-jose is required for admin JWT")

    ttl = ttl_s if ttl_s is not None else (
        _PARTIAL_TOKEN_TTL_S if partial else _ADMIN_JWT_TTL_S
    )
    now = datetime.now(timezone.utc)
    payload = {
        "sub":     str(admin_id),
        "usr":     username,
        "role":    role,
        "partial": partial,
        "iat":     int(now.timestamp()),
        "exp":     int((now + timedelta(seconds=ttl)).timestamp()),
        "jti":     secrets.token_hex(16),   # JWT ID for revocation
    }
    return jwt.encode(payload, settings.admin_jwt_secret, algorithm="HS256")


def verify_admin_jwt(token: str, require_full: bool = True) -> dict[str, Any]:
    """
    Verify and decode an admin JWT.
    require_full=True: rejects partial tokens (TOTP not yet completed).
    Raises AdminAuthError on any verification failure.
    """
    try:
        from jose import jwt, JWTError
    except ImportError:
        raise AdminAuthError(detail="JWT library not available")

    try:
        payload = jwt.decode(
            token,
            settings.admin_jwt_secret,
            algorithms=["HS256"],
            options={"verify_exp": True},
        )
    except JWTError as exc:
        raise AdminAuthError(detail=f"Invalid admin token: {exc}")

    if require_full and payload.get("partial"):
        raise AdminAuthError(detail="TOTP verification required")

    return payload


# ── Admin CRUD ─────────────────────────────────────────────────────────────────

async def create_admin(
    db: AsyncSession,
    username: str,
    email: str,
    password: str,
    role: str = "admin",
    actor_id: int | None = None,
) -> Admin:
    """
    Create a new admin account.
    Password must be ≥16 characters.
    Username must be 3-32 alphanumeric+underscore chars.
    """
    import re
    if len(password) < 16:
        raise ValidationError(
            detail="Admin password must be at least 16 characters",
            user_message="Password too short — minimum 16 characters.",
        )
    if not re.match(r'^[a-z0-9_]{3,32}$', username):
        raise ValidationError(
            detail=f"Invalid admin username: {username!r}",
            user_message="Username must be 3-32 characters (a-z, 0-9, _).",
        )

    # Check uniqueness.
    existing = (await db.execute(
        select(Admin).where(Admin.username == username)
    )).scalar_one_or_none()
    if existing is not None:
        raise ValidationError(
            detail=f"Admin username {username!r} already exists",
            user_message="Username already taken.",
        )

    admin = Admin(
        username=username,
        email=email.lower().strip(),
        password_hash=hash_password(password),
        role=role,
        is_active=True,
        totp_enabled=False,
        failed_attempts=0,
    )
    db.add(admin)
    await db.flush()

    logger.info(
        "admin_created",
        admin_id=admin.id,
        username=username,
        role=role,
        actor_id=actor_id,
    )
    return admin


async def authenticate_admin(
    db: AsyncSession,
    username: str,
    password: str,
) -> tuple[Admin, str]:
    """
    Verify admin username + password.
    Returns (admin, partial_token) if TOTP is enabled.
    Returns (admin, full_token) if TOTP is disabled.

    Raises:
      InvalidCredentialsError — wrong username/password
      AdminLockedError        — account locked due to failed attempts
    """
    admin = (await db.execute(
        select(Admin).where(
            Admin.username == username,
            Admin.is_active == True,
        )
    )).scalar_one_or_none()

    # Constant-time path for non-existent user (prevents enumeration).
    if admin is None:
        hash_password("dummy_constant_time_check_xyzzy")
        raise InvalidCredentialsError(detail="Invalid credentials")

    # Check lockout.
    if admin.locked_until and admin.locked_until > datetime.now(timezone.utc):
        raise AdminLockedError(
            detail=f"Admin {username!r} locked until {admin.locked_until}",
            user_message=(
                f"Account locked due to {_MAX_FAILED_ATTEMPTS} failed attempts. "
                f"Try again after {admin.locked_until.strftime('%H:%M UTC')}."
            ),
        )

    if not verify_password(password, admin.password_hash):
        # Increment failure count.
        admin.failed_attempts = (admin.failed_attempts or 0) + 1
        if admin.failed_attempts >= _MAX_FAILED_ATTEMPTS:
            admin.locked_until = datetime.now(timezone.utc) + timedelta(minutes=_LOCKOUT_MINUTES)
            logger.warning(
                "admin_account_locked",
                admin_id=admin.id,
                username=username,
                locked_until=admin.locked_until.isoformat(),
            )
        await db.flush()
        raise InvalidCredentialsError(detail="Invalid credentials")

    # Reset failure count on successful password verify.
    admin.failed_attempts = 0
    admin.locked_until    = None
    await db.flush()

    is_partial = admin.totp_enabled
    token = _generate_admin_jwt(
        admin_id=admin.id,
        username=admin.username,
        role=admin.role,
        partial=is_partial,
    )

    logger.info(
        "admin_password_verified",
        admin_id=admin.id,
        username=username,
        totp_required=is_partial,
    )
    return admin, token


async def verify_admin_totp(
    db: AsyncSession,
    partial_token: str,
    totp_code: str,
) -> str:
    """
    Complete the two-factor login: verify TOTP code against the partial token.
    Returns a full admin JWT on success.

    Raises:
      AdminAuthError — invalid/expired partial token
      InvalidTOTPError — wrong TOTP code
    """
    payload = verify_admin_jwt(partial_token, require_full=False)
    if not payload.get("partial"):
        raise AdminAuthError(detail="Token is already a full session token")

    admin_id = int(payload["sub"])
    admin = await db.get(Admin, admin_id)
    if admin is None or not admin.is_active:
        raise AdminAuthError(detail="Admin not found or inactive")

    if not admin.totp_enabled or not admin.totp_secret:
        raise AdminAuthError(detail="TOTP not configured for this admin")

    # Decrypt TOTP secret (stored AES-256-GCM encrypted).
    plaintext_secret = decrypt_provider_credential(admin.totp_secret)

    if not verify_totp(plaintext_secret, totp_code):
        logger.warning("admin_totp_failed", admin_id=admin_id)
        raise InvalidTOTPError(
            detail=f"Invalid TOTP code for admin {admin_id}",
            user_message="Invalid authentication code. Please try again.",
        )

    admin.last_login_at = datetime.now(timezone.utc)
    await db.flush()

    full_token = _generate_admin_jwt(
        admin_id=admin.id,
        username=admin.username,
        role=admin.role,
        partial=False,
    )

    logger.info("admin_totp_verified", admin_id=admin_id)
    return full_token


async def setup_admin_totp(
    db: AsyncSession,
    admin_id: int,
) -> dict[str, str]:
    """
    Generate a new TOTP secret for an admin and return the provisioning URI.
    The secret is NOT yet activated — call confirm_admin_totp() to activate.

    Returns dict with keys: secret, uri, qr_data_url (if qrcode installed)
    """
    admin = await db.get(Admin, admin_id)
    if admin is None:
        raise ValidationError(detail=f"Admin {admin_id} not found")

    secret = generate_totp_secret()
    uri    = generate_totp_provisioning_uri(secret, admin.username)

    # Store encrypted pending secret (not yet enabled).
    admin.totp_secret  = encrypt_provider_credential(secret)
    admin.totp_enabled = False   # not enabled until confirmed
    await db.flush()

    logger.info("admin_totp_setup_initiated", admin_id=admin_id)
    return {"secret": secret, "uri": uri}


async def confirm_admin_totp(
    db: AsyncSession,
    admin_id: int,
    totp_code: str,
) -> None:
    """
    Confirm TOTP setup by verifying the first code.
    Activates TOTP for the admin account.
    """
    admin = await db.get(Admin, admin_id)
    if admin is None or not admin.totp_secret:
        raise ValidationError(detail="TOTP not set up for this admin")

    plaintext_secret = decrypt_provider_credential(admin.totp_secret)
    if not verify_totp(plaintext_secret, totp_code):
        raise InvalidTOTPError(
            detail=f"TOTP confirmation failed for admin {admin_id}",
            user_message="Code incorrect — please re-scan the QR code and try again.",
        )

    admin.totp_enabled = True
    await db.flush()

    logger.info("admin_totp_enabled", admin_id=admin_id)


async def rotate_admin_password(
    db: AsyncSession,
    admin_id: int,
    current_password: str,
    new_password: str,
) -> None:
    """
    Rotate an admin's password after verifying the current one.
    New password must be ≥16 chars and differ from current.
    """
    if len(new_password) < 16:
        raise ValidationError(
            detail="New password must be at least 16 characters",
            user_message="New password too short — minimum 16 characters.",
        )

    admin = await db.get(Admin, admin_id)
    if admin is None:
        raise ValidationError(detail=f"Admin {admin_id} not found")

    if not verify_password(current_password, admin.password_hash):
        raise InvalidCredentialsError(detail="Current password incorrect")

    if verify_password(new_password, admin.password_hash):
        raise ValidationError(
            detail="New password must differ from current",
            user_message="New password must be different from your current password.",
        )

    admin.password_hash = hash_password(new_password)
    admin.updated_at    = datetime.now(timezone.utc)
    await db.flush()

    logger.info("admin_password_rotated", admin_id=admin_id)


async def deactivate_admin(
    db: AsyncSession,
    admin_id: int,
    actor_id: int,
) -> None:
    """Deactivate an admin account (soft delete)."""
    admin = await db.get(Admin, admin_id)
    if admin is None:
        raise ValidationError(detail=f"Admin {admin_id} not found")

    admin.is_active = False
    await db.flush()

    logger.info("admin_deactivated", admin_id=admin_id, actor_id=actor_id)
