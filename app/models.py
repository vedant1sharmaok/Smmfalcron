"""SQLAlchemy 2.x models. Wallet balances are cached but the ledger is the source of truth."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false as sa_false,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    from datetime import timezone

    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    first_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    language_code: Mapped[str] = mapped_column(String(8), default="en")
    role: Mapped[str] = mapped_column(String(16), default="user", index=True)
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False)
    terms_accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    privacy_accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    welcome_bonus_granted: Mapped[bool] = mapped_column(Boolean, default=False)
    notify_orders: Mapped[bool] = mapped_column(Boolean, default=True)
    cached_balance_paise: Mapped[int] = mapped_column(BigInteger, default=0)
    cached_reserved_paise: Mapped[int] = mapped_column(BigInteger, default=0)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    referral_code: Mapped[str | None] = mapped_column(String(16), unique=True, nullable=True, index=True)
    referred_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    ledger_entries: Mapped[list["LedgerEntry"]] = relationship(back_populates="user")
    orders: Mapped[list["Order"]] = relationship(back_populates="user")

    @property
    def available_paise(self) -> int:
        return int(self.cached_balance_paise) - int(self.cached_reserved_paise)

    @property
    def display_name(self) -> str:
        parts = [p for p in (self.first_name, self.last_name) if p]
        if parts:
            return " ".join(parts)
        if self.username:
            return f"@{self.username}"
        return str(self.telegram_id)


class LedgerEntry(Base):
    """Immutable wallet ledger. Never UPDATE amount columns after insert."""

    __tablename__ = "ledger_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id"), index=True, nullable=False
    )
    amount_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    entry_type: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    reason: Mapped[str] = mapped_column(String(255), nullable=False)
    reference_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reference_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    balance_after_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reserved_after_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    meta_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    user: Mapped["User"] = relationship(back_populates="ledger_entries")


class Provider(Base):
    __tablename__ = "providers"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    adapter_type: Mapped[str] = mapped_column(String(32), default="mock")
    base_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    encrypted_api_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    health_status: Mapped[str] = mapped_column(String(16), default="unknown")
    last_health_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_health_detail: Mapped[str | None] = mapped_column(String(255), nullable=True)
    markup_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    markup_fixed_paise: Mapped[int | None] = mapped_column(Integer, nullable=True)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=30)
    # provider currency -> INR multiplier applied to synced rates (e.g. 83.0 for a USD panel)
    fx_to_inr: Mapped[float] = mapped_column(Float, default=1.0, server_default="1.0")
    # new services from this provider become sellable immediately (off by default on purpose)
    auto_activate_new: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sa_false())
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_sync_status: Mapped[str | None] = mapped_column(String(160), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    services: Mapped[list["Service"]] = relationship(back_populates="provider")


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    emoji: Mapped[str] = mapped_column(String(8), default="")
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    markup_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    markup_fixed_paise: Mapped[int | None] = mapped_column(Integer, nullable=True)

    services: Mapped[list["Service"]] = relationship(back_populates="category")


class Service(Base):
    __tablename__ = "services"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    category_id: Mapped[str] = mapped_column(String(32), ForeignKey("categories.id"), index=True)
    provider_id: Mapped[str] = mapped_column(String(32), ForeignKey("providers.id"), index=True)
    provider_service_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    service_type: Mapped[str] = mapped_column(String(32), default="default")
    min_qty: Mapped[int] = mapped_column(Integer, default=100)
    max_qty: Mapped[int] = mapped_column(Integer, default=100_000)
    rate_per_1000_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    markup_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    markup_fixed_paise: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_refillable: Mapped[bool] = mapped_column(Boolean, default=True)
    is_cancelable: Mapped[bool] = mapped_column(Boolean, default=True)
    average_time: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # provider-side facts kept separately so admin edits to name/description are never overwritten
    external_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    external_category: Mapped[str | None] = mapped_column(String(120), nullable=True)
    is_removed_upstream: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sa_false())
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    category: Mapped["Category"] = relationship(back_populates="services")
    provider: Mapped["Provider"] = relationship(back_populates="services")
    stars: Mapped[list["StarPrice"]] = relationship(back_populates="service")


class StarPrice(Base):
    """Custom sell rate that outranks every other markup layer."""

    __tablename__ = "star_prices"
    __table_args__ = (
        UniqueConstraint("service_id", "user_id", name="uq_star_service_user"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    service_id: Mapped[str] = mapped_column(String(64), ForeignKey("services.id"), index=True)
    user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id"), nullable=True, index=True
    )
    custom_rate_per_1000_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str] = mapped_column(String(80), default="Star")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    service: Mapped["Service"] = relationship(back_populates="stars")


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(String(24), unique=True, nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id"), index=True, nullable=False
    )
    service_id: Mapped[str] = mapped_column(String(64), ForeignKey("services.id"), index=True)
    provider_id: Mapped[str] = mapped_column(String(32), ForeignKey("providers.id"))
    provider_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    link: Mapped[str] = mapped_column(String(1024), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    extra_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    charge_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cost_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    start_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    remains: Mapped[int | None] = mapped_column(Integer, nullable=True)
    service_snapshot_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    coupon_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    fail_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    refill_count: Mapped[int] = mapped_column(Integer, default=0)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    user: Mapped["User"] = relationship(back_populates="orders")
    service: Mapped["Service"] = relationship()
    events: Mapped[list["OrderEvent"]] = relationship(back_populates="order")

    def extra(self) -> dict[str, Any]:
        if not self.extra_json:
            return {}
        import json

        try:
            data = json.loads(self.extra_json)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}


class OrderEvent(Base):
    __tablename__ = "order_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(Integer, ForeignKey("orders.id"), index=True)
    from_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    to_status: Mapped[str] = mapped_column(String(24), nullable=False)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    order: Mapped["Order"] = relationship(back_populates="events")


class Coupon(Base):
    __tablename__ = "coupons"

    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    discount_type: Mapped[str] = mapped_column(String(16), default="percent")
    value: Mapped[float] = mapped_column(Float, nullable=False)
    max_uses: Mapped[int | None] = mapped_column(Integer, nullable=True)
    used_count: Mapped[int] = mapped_column(Integer, default=0)
    per_user_limit: Mapped[int] = mapped_column(Integer, default=1)
    min_order_paise: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    description: Mapped[str | None] = mapped_column(String(160), nullable=True)


class CouponRedemption(Base):
    __tablename__ = "coupon_redemptions"
    __table_args__ = (UniqueConstraint("coupon_code", "user_id", "order_id", name="uq_coupon_user_order"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    coupon_code: Mapped[str] = mapped_column(String(32), ForeignKey("coupons.code"), index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.telegram_id"), index=True)
    order_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("orders.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(String(24), unique=True, nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.telegram_id"), index=True)
    amount_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    method: Mapped[str] = mapped_column(String(24), default="mock")
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    provider_ref: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    currency: Mapped[str] = mapped_column(String(3), default="INR", server_default="INR")
    checkout_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    scope: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    response_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, server_default=func.now())


class Reseller(Base):
    """Active resellers buy at a discounted sell rate (default 12%)."""

    __tablename__ = "resellers"

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id"), primary_key=True
    )
    tier: Mapped[str] = mapped_column(String(16), default="bronze")
    discount_bp: Mapped[int] = mapped_column(Integer, default=1200)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    notes: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class ResellerOrder(Base):
    """Optional link of an order placed at reseller price."""

    __tablename__ = "reseller_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    reseller_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("resellers.user_id"), index=True, nullable=False
    )
    order_id: Mapped[int] = mapped_column(Integer, ForeignKey("orders.id"), unique=True, nullable=False)
    charge_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    retail_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Promoter(Base):
    __tablename__ = "promoters"

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id"), primary_key=True
    )
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    notes: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class ReferralClick(Base):
    """Every /start hit with a referral or promoter code."""

    __tablename__ = "referral_clicks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(16), index=True, nullable=False)
    referrer_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    visitor_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    is_signup: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)


class RoleApplication(Base):
    __tablename__ = "role_applications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.telegram_id"), index=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    pitch: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class PayoutRequest(Base):
    __tablename__ = "payout_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(String(24), unique=True, nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.telegram_id"), index=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    amount_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Ad(Base):
    __tablename__ = "ads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    placement: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(80), nullable=False)
    caption: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    image_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort: Mapped[int] = mapped_column(Integer, default=0)


class PaymentEvent(Base):
    """Every verified (or rejected) gateway callback. The unique (gateway, event_id) pair is
    the webhook replay guard: a re-delivered event can never credit twice."""

    __tablename__ = "payment_events"
    __table_args__ = (UniqueConstraint("gateway", "event_id", name="uq_payment_event"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    gateway: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    event_id: Mapped[str] = mapped_column(String(80), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payment_public_id: Mapped[str | None] = mapped_column(String(24), nullable=True, index=True)
    amount_paise: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    detail: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)


class AuditLog(Base):
    """Append-only trail of sensitive actions: who, what, old -> new, why."""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor: Mapped[str] = mapped_column(String(48), nullable=False, index=True)  # telegram id, "system", "cli"
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    target: Mapped[str | None] = mapped_column(String(80), nullable=True)
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)


class SyncRun(Base):
    __tablename__ = "sync_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # ok | aborted | error
    added: Mapped[int] = mapped_column(Integer, default=0)
    updated: Mapped[int] = mapped_column(Integer, default=0)
    removed: Mapped[int] = mapped_column(Integer, default=0)
    skipped_invalid: Mapped[int] = mapped_column(Integer, default=0)
    detail: Mapped[str | None] = mapped_column(String(255), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)



# Canonical order status labels used by the bot and API (plain text, no custom emoji).
ORDER_STATUS_LABELS: dict[str, str] = {
    "pending": "Pending",
    "awaiting_provider": "Submitting",
    "processing": "Processing",
    "in_progress": "In progress",
    "completed": "Completed",
    "partial": "Partial",
    "canceled": "Canceled",
    "cancelled": "Canceled",
    "failed": "Failed",
    "refunded": "Refunded",
    "refilling": "Refilling",
    "review": "Under review",
}

# "review": provider outcome unknown (timeout after submit). Funds stay reserved until an admin resolves it.
REVIEW_STATUS = "review"
TERMINAL_STATUSES = frozenset({"completed", "canceled", "cancelled", "failed", "refunded"})
ACTIVE_STATUSES = frozenset({"pending", "awaiting_provider", "processing", "in_progress", "partial", "refilling"})
REFILLABLE_STATUSES = frozenset({"completed", "partial"})
CANCELABLE_STATUSES = frozenset({"pending", "awaiting_provider", "processing", "in_progress"})

KILL_NEW_ORDERS = "kill_new_orders"
KILL_PAYMENTS = "kill_payments"
KILL_REFILLS = "kill_refills"
KILL_READ_ONLY = "read_only"
KILL_MAINTENANCE = "global_maintenance"
SETTING_GLOBAL_MARKUP = "global_markup_percent"
SETTING_MIN_MARGIN = "min_margin_percent"
SETTING_WELCOME_BONUS = "welcome_bonus_paise"

AD_PLACEMENTS = ("home", "services", "wallet", "orders", "profile", "rewards")
DEFAULT_RESELLER_DISCOUNT_BP = 1200
RESELLER_MIN_MARGIN_PERCENT = 10.0
REFERRAL_ORDER_COMMISSION_PERCENT = 8.0
REFERRAL_DEPOSIT_COMMISSION_PERCENT = 5.0
REFERRAL_ORDER_CAP = 3
MIN_PAYOUT_PAISE = 10_000  # ₹100.00
