"""
SQLAlchemy ORM models — all platform entities.

Import from here, never from individual modules:
  from app.core.models import User, Order, Wallet, ...
"""
from __future__ import annotations
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import (
    BigInteger, Boolean, DateTime, ForeignKey, Index, Integer,
    Numeric, String, Text, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


# ── Users ─────────────────────────────────────────────────────────────────────

class User(Base):
    __tablename__ = "users"
    id:               Mapped[int]           = mapped_column(BigInteger, primary_key=True)
    telegram_id:      Mapped[int]           = mapped_column(BigInteger, unique=True, nullable=False)
    first_name:       Mapped[Optional[str]] = mapped_column(String(128))
    last_name:        Mapped[Optional[str]] = mapped_column(String(128))
    username:         Mapped[Optional[str]] = mapped_column(String(64))
    language_code:    Mapped[Optional[str]] = mapped_column(String(8))
    is_active:        Mapped[bool]          = mapped_column(Boolean, default=True)
    is_banned:        Mapped[bool]          = mapped_column(Boolean, default=False)
    is_premium:       Mapped[bool]          = mapped_column(Boolean, default=False)
    policy_version:   Mapped[int]           = mapped_column(Integer, default=0)
    last_activity_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at:       Mapped[datetime]      = mapped_column(DateTime(timezone=True))
    updated_at:       Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


# ── Wallet ────────────────────────────────────────────────────────────────────

class Wallet(Base):
    __tablename__ = "wallets"
    id:         Mapped[int]     = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id:    Mapped[int]     = mapped_column(BigInteger, ForeignKey("users.id"), unique=True)
    balance:    Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal("0"))
    currency:   Mapped[str]     = mapped_column(String(3), default="INR")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class WalletTransaction(Base):
    __tablename__ = "wallet_transactions"
    id:               Mapped[str]     = mapped_column(String(36), primary_key=True)
    wallet_id:        Mapped[int]     = mapped_column(Integer, ForeignKey("wallets.id"))
    tx_type:          Mapped[str]     = mapped_column(String(32))
    amount:           Mapped[Decimal] = mapped_column(Numeric(18, 4))
    balance_before:   Mapped[Decimal] = mapped_column(Numeric(18, 4))
    balance_after:    Mapped[Decimal] = mapped_column(Numeric(18, 4))
    idempotency_key:  Mapped[str]     = mapped_column(String(128), unique=True)
    reference_id:     Mapped[Optional[str]] = mapped_column(String(128))
    reason:           Mapped[Optional[str]] = mapped_column(Text)
    created_at:       Mapped[datetime] = mapped_column(DateTime(timezone=True))


# ── Providers ─────────────────────────────────────────────────────────────────

class Provider(Base):
    __tablename__ = "providers"
    id:                  Mapped[int]           = mapped_column(Integer, primary_key=True, autoincrement=True)
    name:                Mapped[str]           = mapped_column(String(128))
    slug:                Mapped[str]           = mapped_column(String(64), unique=True)
    endpoint:            Mapped[str]           = mapped_column(String(512))
    credentials_enc:     Mapped[Optional[bytes]] = mapped_column(Text)
    currency:            Mapped[str]           = mapped_column(String(8), default="USD")
    is_active:           Mapped[bool]          = mapped_column(Boolean, default=True)
    health_status:       Mapped[str]           = mapped_column(String(32), default="unknown")
    last_health_check_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    priority:            Mapped[int]           = mapped_column(Integer, default=0)
    profit_pct:          Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 4))
    last_sync_at:        Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at:          Mapped[datetime]      = mapped_column(DateTime(timezone=True))


class ProviderService(Base):
    __tablename__ = "provider_services"
    id:               Mapped[int]           = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider_id:      Mapped[int]           = mapped_column(Integer, ForeignKey("providers.id"))
    provider_svc_id:  Mapped[str]           = mapped_column(String(64))
    raw_name:         Mapped[str]           = mapped_column(String(512))
    category:         Mapped[str]           = mapped_column(String(128))
    rate:             Mapped[Decimal]       = mapped_column(Numeric(18, 6))
    min_qty:          Mapped[int]           = mapped_column(Integer)
    max_qty:          Mapped[int]           = mapped_column(Integer)
    refill:           Mapped[bool]          = mapped_column(Boolean, default=False)
    cancel:           Mapped[bool]          = mapped_column(Boolean, default=False)
    drip_feed:        Mapped[bool]          = mapped_column(Boolean, default=False)
    is_active:        Mapped[bool]          = mapped_column(Boolean, default=True)
    last_seen_at:     Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("provider_id", "provider_svc_id"),)


# ── Services / Catalog ────────────────────────────────────────────────────────

class Category(Base):
    __tablename__ = "categories"
    id:        Mapped[int]           = mapped_column(Integer, primary_key=True, autoincrement=True)
    name:      Mapped[str]           = mapped_column(String(128))
    slug:      Mapped[str]           = mapped_column(String(64), unique=True)
    is_active: Mapped[bool]          = mapped_column(Boolean, default=True)
    sort_order: Mapped[int]          = mapped_column(Integer, default=0)
    profit_pct: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 4))


class Service(Base):
    __tablename__ = "services"
    id:                  Mapped[int]           = mapped_column(Integer, primary_key=True, autoincrement=True)
    public_id:           Mapped[str]           = mapped_column(String(16), unique=True)
    display_name:        Mapped[str]           = mapped_column(String(512))
    admin_name:          Mapped[Optional[str]] = mapped_column(String(512))
    category_id:         Mapped[int]           = mapped_column(Integer, ForeignKey("categories.id"))
    custom_price:        Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6))
    profit_pct:          Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 4))
    is_active:           Mapped[bool]          = mapped_column(Boolean, default=True)
    ordering_enabled:    Mapped[bool]          = mapped_column(Boolean, default=True)
    refill_enabled:      Mapped[bool]          = mapped_column(Boolean, default=False)
    cancel_enabled:      Mapped[bool]          = mapped_column(Boolean, default=False)
    requires_premium:    Mapped[bool]          = mapped_column(Boolean, default=False)
    sort_order:          Mapped[int]           = mapped_column(Integer, default=0)
    created_at:          Mapped[datetime]      = mapped_column(DateTime(timezone=True))
    updated_at:          Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class ServiceProviderMapping(Base):
    __tablename__ = "service_provider_mappings"
    id:               Mapped[int]  = mapped_column(Integer, primary_key=True, autoincrement=True)
    service_id:       Mapped[int]  = mapped_column(Integer, ForeignKey("services.id"))
    provider_id:      Mapped[int]  = mapped_column(Integer, ForeignKey("providers.id"))
    provider_svc_id:  Mapped[str]  = mapped_column(String(64))
    is_primary:       Mapped[bool] = mapped_column(Boolean, default=True)
    routing_weight:   Mapped[int]  = mapped_column(Integer, default=100)
    __table_args__ = (UniqueConstraint("service_id", "provider_id"),)


# ── Pricing ───────────────────────────────────────────────────────────────────

class PricingRule(Base):
    __tablename__ = "pricing_rules"
    id:             Mapped[int]     = mapped_column(Integer, primary_key=True, autoincrement=True)
    scope:          Mapped[str]     = mapped_column(String(32))  # global|provider|category|service|star
    scope_id:       Mapped[Optional[int]] = mapped_column(Integer)
    rule_type:      Mapped[str]     = mapped_column(String(32))  # percentage|fixed_markup|fixed_price
    value:          Mapped[Decimal] = mapped_column(Numeric(18, 6))
    min_margin_pct: Mapped[Decimal] = mapped_column(Numeric(8, 4), default=Decimal("5"))
    currency:       Mapped[str]     = mapped_column(String(8), default="INR")
    is_active:      Mapped[bool]    = mapped_column(Boolean, default=True)
    created_at:     Mapped[datetime] = mapped_column(DateTime(timezone=True))


# ── Orders ────────────────────────────────────────────────────────────────────

class Order(Base):
    __tablename__ = "orders"
    id:                  Mapped[str]           = mapped_column(String(36), primary_key=True)
    public_ref:          Mapped[str]           = mapped_column(String(32), unique=True)
    user_id:             Mapped[int]           = mapped_column(BigInteger, ForeignKey("users.id"))
    tenant_id:           Mapped[Optional[int]] = mapped_column(Integer)
    service_id:          Mapped[int]           = mapped_column(Integer, ForeignKey("services.id"))
    provider_id:         Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("providers.id"))
    provider_order_id:   Mapped[Optional[str]] = mapped_column(String(128))
    provider_svc_id:     Mapped[Optional[str]] = mapped_column(String(64))
    status:              Mapped[str]           = mapped_column(String(32), default="pending")
    quantity:            Mapped[int]           = mapped_column(Integer)
    link:                Mapped[Optional[str]] = mapped_column(Text)
    price_charged:       Mapped[Decimal]       = mapped_column(Numeric(18, 4))
    provider_cost:       Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6))
    currency:            Mapped[str]           = mapped_column(String(8), default="INR")
    idempotency_key:     Mapped[str]           = mapped_column(String(128), unique=True)
    source:              Mapped[str]           = mapped_column(String(32), default="bot")
    priority:            Mapped[int]           = mapped_column(Integer, default=3)
    refill_eligible:     Mapped[bool]          = mapped_column(Boolean, default=False)
    cancel_eligible:     Mapped[bool]          = mapped_column(Boolean, default=False)
    remains:             Mapped[Optional[int]] = mapped_column(Integer)
    start_count:         Mapped[Optional[int]] = mapped_column(Integer)
    created_at:          Mapped[datetime]      = mapped_column(DateTime(timezone=True))
    updated_at:          Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    completed_at:        Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


# ── Payments ──────────────────────────────────────────────────────────────────

class Payment(Base):
    __tablename__ = "payments"
    id:               Mapped[int]           = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id:          Mapped[int]           = mapped_column(BigInteger, ForeignKey("users.id"))
    amount:           Mapped[Decimal]       = mapped_column(Numeric(18, 4))
    currency:         Mapped[str]           = mapped_column(String(8), default="INR")
    provider:         Mapped[str]           = mapped_column(String(32))
    provider_ref:     Mapped[str]           = mapped_column(String(128))
    provider_txn_id:  Mapped[Optional[str]] = mapped_column(String(128))
    status:           Mapped[str]           = mapped_column(String(32), default="pending")
    webhook_verified: Mapped[bool]          = mapped_column(Boolean, default=False)
    verified_at:      Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at:       Mapped[datetime]      = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("provider", "provider_ref"),)


# ── Sessions ──────────────────────────────────────────────────────────────────

class UserSession(Base):
    __tablename__ = "user_sessions"
    id:         Mapped[str]  = mapped_column(String(64), primary_key=True)
    user_id:    Mapped[int]  = mapped_column(BigInteger, ForeignKey("users.id"))
    session_type: Mapped[str] = mapped_column(String(32))  # bot|miniapp|api
    is_active:  Mapped[bool] = mapped_column(Boolean, default=True)
    ip_hash:    Mapped[Optional[str]] = mapped_column(String(64))
    created_at: Mapped[datetime]      = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime]      = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


# ── Coupons ───────────────────────────────────────────────────────────────────

class Coupon(Base):
    __tablename__ = "coupons"
    id:               Mapped[int]           = mapped_column(Integer, primary_key=True, autoincrement=True)
    code:             Mapped[str]           = mapped_column(String(32), unique=True)
    discount_type:    Mapped[str]           = mapped_column(String(32))   # percentage|fixed
    discount_value:   Mapped[Decimal]       = mapped_column(Numeric(18, 4))
    max_discount:     Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 4))
    min_spend:        Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 4))
    total_limit:      Mapped[Optional[int]] = mapped_column(Integer)
    per_user_limit:   Mapped[int]           = mapped_column(Integer, default=1)
    expires_at:       Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    is_active:        Mapped[bool]          = mapped_column(Boolean, default=True)
    is_transferable:  Mapped[bool]          = mapped_column(Boolean, default=False)
    owner_user_id:    Mapped[Optional[int]] = mapped_column(BigInteger)
    service_ids:      Mapped[Optional[list]] = mapped_column(JSONB)
    category_ids:     Mapped[Optional[list]] = mapped_column(JSONB)
    created_at:       Mapped[datetime]      = mapped_column(DateTime(timezone=True))


class CouponRedemption(Base):
    __tablename__ = "coupon_redemptions"
    id:               Mapped[int]     = mapped_column(Integer, primary_key=True, autoincrement=True)
    coupon_id:        Mapped[int]     = mapped_column(Integer, ForeignKey("coupons.id"))
    user_id:          Mapped[int]     = mapped_column(BigInteger, ForeignKey("users.id"))
    order_id:         Mapped[str]     = mapped_column(String(36))
    discount_applied: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    idempotency_key:  Mapped[str]     = mapped_column(String(256), unique=True)
    created_at:       Mapped[datetime] = mapped_column(DateTime(timezone=True))


# ── Reseller ──────────────────────────────────────────────────────────────────

class ResellerProfile(Base):
    __tablename__ = "reseller_profiles"
    id:                  Mapped[int]     = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id:             Mapped[int]     = mapped_column(BigInteger, ForeignKey("users.id"), unique=True)
    plan_name:           Mapped[str]     = mapped_column(String(64), default="standard")
    discount_pct:        Mapped[Decimal] = mapped_column(Numeric(8, 4), default=Decimal("0"))
    min_margin_pct:      Mapped[Decimal] = mapped_column(Numeric(8, 4), default=Decimal("5"))
    is_active:           Mapped[bool]    = mapped_column(Boolean, default=True)
    monthly_order_limit: Mapped[Optional[int]] = mapped_column(Integer)
    notes:               Mapped[Optional[str]]  = mapped_column(Text)
    extra_config:        Mapped[Optional[dict]] = mapped_column(JSONB)
    created_at:          Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at:          Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


# ── Admin ─────────────────────────────────────────────────────────────────────

class Admin(Base):
    __tablename__ = "admins"
    id:              Mapped[int]           = mapped_column(Integer, primary_key=True, autoincrement=True)
    username:        Mapped[str]           = mapped_column(String(64), unique=True)
    email:           Mapped[str]           = mapped_column(String(255), unique=True)
    password_hash:   Mapped[str]           = mapped_column(String(255))
    totp_secret:     Mapped[Optional[bytes]] = mapped_column(Text)
    totp_enabled:    Mapped[bool]          = mapped_column(Boolean, default=False)
    is_active:       Mapped[bool]          = mapped_column(Boolean, default=True)
    role:            Mapped[str]           = mapped_column(String(64), default="admin")
    failed_attempts: Mapped[int]           = mapped_column(Integer, default=0)
    locked_until:    Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_login_at:   Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at:      Mapped[datetime]      = mapped_column(DateTime(timezone=True))
    updated_at:      Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


# ── Tenants ───────────────────────────────────────────────────────────────────

class Tenant(Base):
    __tablename__ = "tenants"
    id:             Mapped[int]           = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_user_id:  Mapped[int]           = mapped_column(BigInteger, ForeignKey("users.id"))
    slug:           Mapped[str]           = mapped_column(String(64), unique=True)
    display_name:   Mapped[str]           = mapped_column(String(128))
    bot_token_enc:  Mapped[Optional[bytes]] = mapped_column(Text)
    bot_username:   Mapped[Optional[str]] = mapped_column(String(64))
    webhook_secret: Mapped[Optional[str]] = mapped_column(String(64))
    plan:           Mapped[str]           = mapped_column(String(32), default="basic")
    is_active:      Mapped[bool]          = mapped_column(Boolean, default=False)
    is_deleted:     Mapped[bool]          = mapped_column(Boolean, default=False)
    config:         Mapped[Optional[dict]] = mapped_column(JSONB)
    created_at:     Mapped[datetime]      = mapped_column(DateTime(timezone=True))
    updated_at:     Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class TenantUser(Base):
    __tablename__ = "tenant_users"
    id:               Mapped[int]           = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id:        Mapped[int]           = mapped_column(Integer, ForeignKey("tenants.id"))
    telegram_id:      Mapped[int]           = mapped_column(BigInteger)
    first_name:       Mapped[Optional[str]] = mapped_column(String(128))
    username:         Mapped[Optional[str]] = mapped_column(String(64))
    is_active:        Mapped[bool]          = mapped_column(Boolean, default=True)
    is_banned:        Mapped[bool]          = mapped_column(Boolean, default=False)
    wallet_balance:   Mapped[Decimal]       = mapped_column(Numeric(18, 4), default=Decimal("0"))
    last_activity_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at:       Mapped[datetime]      = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("tenant_id", "telegram_id"),)


# ── Premium ───────────────────────────────────────────────────────────────────

class UserPremium(Base):
    __tablename__ = "user_premiums"
    id:           Mapped[int]     = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id:      Mapped[int]     = mapped_column(BigInteger, ForeignKey("users.id"))
    plan:         Mapped[str]     = mapped_column(String(32))
    status:       Mapped[str]     = mapped_column(String(32), default="active")
    starts_at:    Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at:   Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    payment_ref:  Mapped[Optional[str]] = mapped_column(String(128))
    auto_renew:   Mapped[bool]    = mapped_column(Boolean, default=True)
    cancelled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    expired_at:   Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at:   Mapped[datetime] = mapped_column(DateTime(timezone=True))


# ── CMS ───────────────────────────────────────────────────────────────────────

class ContentBlock(Base):
    __tablename__ = "content_blocks"
    id:         Mapped[int]           = mapped_column(Integer, primary_key=True, autoincrement=True)
    key:        Mapped[str]           = mapped_column(String(64))
    locale:     Mapped[str]           = mapped_column(String(8), default="en")
    content:    Mapped[str]           = mapped_column(Text)
    is_active:  Mapped[bool]          = mapped_column(Boolean, default=True)
    version:    Mapped[int]           = mapped_column(Integer, default=1)
    updated_by: Mapped[Optional[int]] = mapped_column(Integer)
    created_at: Mapped[datetime]      = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime]      = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("key", "locale"),)


class Broadcast(Base):
    __tablename__ = "broadcasts"
    id:            Mapped[int]           = mapped_column(Integer, primary_key=True, autoincrement=True)
    title:         Mapped[str]           = mapped_column(String(256))
    message:       Mapped[str]           = mapped_column(Text)
    segment:       Mapped[str]           = mapped_column(String(32), default="all")
    status:        Mapped[str]           = mapped_column(String(32), default="pending")
    scheduled_at:  Mapped[datetime]      = mapped_column(DateTime(timezone=True))
    completed_at:  Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_by:    Mapped[Optional[int]] = mapped_column(Integer)
    created_at:    Mapped[datetime]      = mapped_column(DateTime(timezone=True))
    sent_count:    Mapped[int]           = mapped_column(Integer, default=0)
    failed_count:  Mapped[int]           = mapped_column(Integer, default=0)
    pending_count: Mapped[int]           = mapped_column(Integer, default=0)


# ── Audit log ─────────────────────────────────────────────────────────────────

class AuditLog(Base):
    __tablename__ = "audit_log"
    id:          Mapped[int]           = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    actor_id:    Mapped[Optional[int]] = mapped_column(BigInteger)
    action:      Mapped[str]           = mapped_column(String(128))
    resource:    Mapped[str]           = mapped_column(String(64))
    resource_id: Mapped[Optional[str]] = mapped_column(String(128))
    details:     Mapped[Optional[dict]] = mapped_column(JSONB)
    ip_hash:     Mapped[Optional[str]] = mapped_column(String(64))
    tenant_id:   Mapped[Optional[int]] = mapped_column(Integer)
    created_at:  Mapped[datetime]      = mapped_column(DateTime(timezone=True))
