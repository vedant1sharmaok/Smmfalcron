"""Initial schema — all platform tables.

Revision ID: 0001
Revises:
Create Date: 2024-08-13 00:00:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers
revision = "0001"
down_revision = None
branch_labels = None
depends_on    = None


def upgrade() -> None:
    # ── users ─────────────────────────────────────────────────────────────────
    op.create_table(
        "users",
        sa.Column("id",             sa.BigInteger(), primary_key=True),
        sa.Column("telegram_id",    sa.BigInteger(), nullable=False, unique=True),
        sa.Column("first_name",     sa.String(128)),
        sa.Column("last_name",      sa.String(128)),
        sa.Column("username",       sa.String(64)),
        sa.Column("language_code",  sa.String(8)),
        sa.Column("is_active",      sa.Boolean(), default=True,  nullable=False),
        sa.Column("is_banned",      sa.Boolean(), default=False, nullable=False),
        sa.Column("is_premium",     sa.Boolean(), default=False, nullable=False),
        sa.Column("policy_version", sa.Integer(), default=0,     nullable=False),
        sa.Column("last_activity_at", sa.DateTime(timezone=True)),
        sa.Column("created_at",     sa.DateTime(timezone=True),  nullable=False),
        sa.Column("updated_at",     sa.DateTime(timezone=True)),
    )

    # ── wallets ───────────────────────────────────────────────────────────────
    op.create_table(
        "wallets",
        sa.Column("id",         sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id",    sa.BigInteger(), sa.ForeignKey("users.id"), unique=True, nullable=False),
        sa.Column("balance",    sa.Numeric(18, 4), default=0,     nullable=False),
        sa.Column("currency",   sa.String(3),      default="INR", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
    )

    # ── wallet_transactions ───────────────────────────────────────────────────
    op.create_table(
        "wallet_transactions",
        sa.Column("id",               sa.String(36),   primary_key=True),
        sa.Column("wallet_id",        sa.Integer(),    sa.ForeignKey("wallets.id"), nullable=False),
        sa.Column("tx_type",          sa.String(32),   nullable=False),
        sa.Column("amount",           sa.Numeric(18, 4), nullable=False),
        sa.Column("balance_before",   sa.Numeric(18, 4), nullable=False),
        sa.Column("balance_after",    sa.Numeric(18, 4), nullable=False),
        sa.Column("idempotency_key",  sa.String(128),  nullable=False, unique=True),
        sa.Column("reference_id",     sa.String(128)),
        sa.Column("reason",           sa.Text()),
        sa.Column("created_at",       sa.DateTime(timezone=True), nullable=False),
    )

    # ── providers ─────────────────────────────────────────────────────────────
    op.create_table(
        "providers",
        sa.Column("id",                    sa.Integer(),    primary_key=True, autoincrement=True),
        sa.Column("name",                  sa.String(128),  nullable=False),
        sa.Column("slug",                  sa.String(64),   nullable=False, unique=True),
        sa.Column("endpoint",              sa.String(512),  nullable=False),
        sa.Column("credentials_enc",       sa.Text()),
        sa.Column("currency",              sa.String(8),    default="USD"),
        sa.Column("is_active",             sa.Boolean(),    default=True),
        sa.Column("health_status",         sa.String(32),   default="unknown"),
        sa.Column("last_health_check_at",  sa.DateTime(timezone=True)),
        sa.Column("priority",              sa.Integer(),    default=0),
        sa.Column("profit_pct",            sa.Numeric(8, 4)),
        sa.Column("last_sync_at",          sa.DateTime(timezone=True)),
        sa.Column("created_at",            sa.DateTime(timezone=True), nullable=False),
    )

    # ── provider_services ─────────────────────────────────────────────────────
    op.create_table(
        "provider_services",
        sa.Column("id",              sa.Integer(),    primary_key=True, autoincrement=True),
        sa.Column("provider_id",     sa.Integer(),    sa.ForeignKey("providers.id"), nullable=False),
        sa.Column("provider_svc_id", sa.String(64),   nullable=False),
        sa.Column("raw_name",        sa.String(512),  nullable=False),
        sa.Column("category",        sa.String(128),  nullable=False),
        sa.Column("rate",            sa.Numeric(18, 6), nullable=False),
        sa.Column("min_qty",         sa.Integer(),    nullable=False),
        sa.Column("max_qty",         sa.Integer(),    nullable=False),
        sa.Column("refill",          sa.Boolean(),    default=False),
        sa.Column("cancel",          sa.Boolean(),    default=False),
        sa.Column("drip_feed",       sa.Boolean(),    default=False),
        sa.Column("is_active",       sa.Boolean(),    default=True),
        sa.Column("last_seen_at",    sa.DateTime(timezone=True)),
        sa.UniqueConstraint("provider_id", "provider_svc_id"),
    )

    # ── categories ────────────────────────────────────────────────────────────
    op.create_table(
        "categories",
        sa.Column("id",         sa.Integer(),   primary_key=True, autoincrement=True),
        sa.Column("name",       sa.String(128), nullable=False),
        sa.Column("slug",       sa.String(64),  nullable=False, unique=True),
        sa.Column("is_active",  sa.Boolean(),   default=True),
        sa.Column("sort_order", sa.Integer(),   default=0),
        sa.Column("profit_pct", sa.Numeric(8, 4)),
    )

    # ── services ──────────────────────────────────────────────────────────────
    op.create_table(
        "services",
        sa.Column("id",               sa.Integer(),    primary_key=True, autoincrement=True),
        sa.Column("public_id",        sa.String(16),   nullable=False, unique=True),
        sa.Column("display_name",     sa.String(512),  nullable=False),
        sa.Column("admin_name",       sa.String(512)),
        sa.Column("category_id",      sa.Integer(),    sa.ForeignKey("categories.id"), nullable=False),
        sa.Column("custom_price",     sa.Numeric(18, 6)),
        sa.Column("profit_pct",       sa.Numeric(8, 4)),
        sa.Column("is_active",        sa.Boolean(),    default=True),
        sa.Column("ordering_enabled", sa.Boolean(),    default=True),
        sa.Column("refill_enabled",   sa.Boolean(),    default=False),
        sa.Column("cancel_enabled",   sa.Boolean(),    default=False),
        sa.Column("requires_premium", sa.Boolean(),    default=False),
        sa.Column("sort_order",       sa.Integer(),    default=0),
        sa.Column("created_at",       sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at",       sa.DateTime(timezone=True)),
    )

    # ── service_provider_mappings ─────────────────────────────────────────────
    op.create_table(
        "service_provider_mappings",
        sa.Column("id",              sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("service_id",      sa.Integer(), sa.ForeignKey("services.id"),  nullable=False),
        sa.Column("provider_id",     sa.Integer(), sa.ForeignKey("providers.id"), nullable=False),
        sa.Column("provider_svc_id", sa.String(64), nullable=False),
        sa.Column("is_primary",      sa.Boolean(), default=True),
        sa.Column("routing_weight",  sa.Integer(), default=100),
        sa.UniqueConstraint("service_id", "provider_id"),
    )

    # ── pricing_rules ─────────────────────────────────────────────────────────
    op.create_table(
        "pricing_rules",
        sa.Column("id",             sa.Integer(),     primary_key=True, autoincrement=True),
        sa.Column("scope",          sa.String(32),    nullable=False),
        sa.Column("scope_id",       sa.Integer()),
        sa.Column("rule_type",      sa.String(32),    nullable=False),
        sa.Column("value",          sa.Numeric(18, 6), nullable=False),
        sa.Column("min_margin_pct", sa.Numeric(8, 4),  default=5),
        sa.Column("currency",       sa.String(8),      default="INR"),
        sa.Column("is_active",      sa.Boolean(),      default=True),
        sa.Column("created_at",     sa.DateTime(timezone=True), nullable=False),
    )

    # ── orders ────────────────────────────────────────────────────────────────
    op.create_table(
        "orders",
        sa.Column("id",                 sa.String(36),    primary_key=True),
        sa.Column("public_ref",         sa.String(32),    nullable=False, unique=True),
        sa.Column("user_id",            sa.BigInteger(),  sa.ForeignKey("users.id"), nullable=False),
        sa.Column("tenant_id",          sa.Integer()),
        sa.Column("service_id",         sa.Integer(),     sa.ForeignKey("services.id"), nullable=False),
        sa.Column("provider_id",        sa.Integer(),     sa.ForeignKey("providers.id")),
        sa.Column("provider_order_id",  sa.String(128)),
        sa.Column("provider_svc_id",    sa.String(64)),
        sa.Column("status",             sa.String(32),    default="pending", nullable=False),
        sa.Column("quantity",           sa.Integer(),     nullable=False),
        sa.Column("link",               sa.Text()),
        sa.Column("price_charged",      sa.Numeric(18, 4), nullable=False),
        sa.Column("provider_cost",      sa.Numeric(18, 6)),
        sa.Column("currency",           sa.String(8),     default="INR"),
        sa.Column("idempotency_key",    sa.String(128),   nullable=False, unique=True),
        sa.Column("source",             sa.String(32),    default="bot"),
        sa.Column("priority",           sa.Integer(),     default=3),
        sa.Column("refill_eligible",    sa.Boolean(),     default=False),
        sa.Column("cancel_eligible",    sa.Boolean(),     default=False),
        sa.Column("remains",            sa.Integer()),
        sa.Column("start_count",        sa.Integer()),
        sa.Column("created_at",         sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at",         sa.DateTime(timezone=True)),
        sa.Column("completed_at",       sa.DateTime(timezone=True)),
    )

    # ── payments ──────────────────────────────────────────────────────────────
    op.create_table(
        "payments",
        sa.Column("id",               sa.Integer(),    primary_key=True, autoincrement=True),
        sa.Column("user_id",          sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("amount",           sa.Numeric(18, 4), nullable=False),
        sa.Column("currency",         sa.String(8),    default="INR"),
        sa.Column("provider",         sa.String(32),   nullable=False),
        sa.Column("provider_ref",     sa.String(128),  nullable=False),
        sa.Column("provider_txn_id",  sa.String(128)),
        sa.Column("status",           sa.String(32),   default="pending"),
        sa.Column("webhook_verified", sa.Boolean(),    default=False),
        sa.Column("verified_at",      sa.DateTime(timezone=True)),
        sa.Column("created_at",       sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("provider", "provider_ref"),
    )

    # ── user_sessions ─────────────────────────────────────────────────────────
    op.create_table(
        "user_sessions",
        sa.Column("id",           sa.String(64),   primary_key=True),
        sa.Column("user_id",      sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("session_type", sa.String(32),   nullable=False),
        sa.Column("is_active",    sa.Boolean(),    default=True),
        sa.Column("ip_hash",      sa.String(64)),
        sa.Column("created_at",   sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at",   sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
    )

    # ── coupons ───────────────────────────────────────────────────────────────
    op.create_table(
        "coupons",
        sa.Column("id",              sa.Integer(),    primary_key=True, autoincrement=True),
        sa.Column("code",            sa.String(32),   nullable=False, unique=True),
        sa.Column("discount_type",   sa.String(32),   nullable=False),
        sa.Column("discount_value",  sa.Numeric(18, 4), nullable=False),
        sa.Column("max_discount",    sa.Numeric(18, 4)),
        sa.Column("min_spend",       sa.Numeric(18, 4)),
        sa.Column("total_limit",     sa.Integer()),
        sa.Column("per_user_limit",  sa.Integer(),    default=1),
        sa.Column("expires_at",      sa.DateTime(timezone=True)),
        sa.Column("is_active",       sa.Boolean(),    default=True),
        sa.Column("is_transferable", sa.Boolean(),    default=False),
        sa.Column("owner_user_id",   sa.BigInteger()),
        sa.Column("service_ids",     postgresql.JSONB()),
        sa.Column("category_ids",    postgresql.JSONB()),
        sa.Column("created_at",      sa.DateTime(timezone=True), nullable=False),
    )

    # ── coupon_redemptions ────────────────────────────────────────────────────
    op.create_table(
        "coupon_redemptions",
        sa.Column("id",               sa.Integer(),    primary_key=True, autoincrement=True),
        sa.Column("coupon_id",        sa.Integer(),    sa.ForeignKey("coupons.id"), nullable=False),
        sa.Column("user_id",          sa.BigInteger(), sa.ForeignKey("users.id"),   nullable=False),
        sa.Column("order_id",         sa.String(36),   nullable=False),
        sa.Column("discount_applied", sa.Numeric(18, 4), nullable=False),
        sa.Column("idempotency_key",  sa.String(256),  nullable=False, unique=True),
        sa.Column("created_at",       sa.DateTime(timezone=True), nullable=False),
    )

    # ── reseller_profiles ─────────────────────────────────────────────────────
    op.create_table(
        "reseller_profiles",
        sa.Column("id",                  sa.Integer(),    primary_key=True, autoincrement=True),
        sa.Column("user_id",             sa.BigInteger(), sa.ForeignKey("users.id"), unique=True, nullable=False),
        sa.Column("plan_name",           sa.String(64),   default="standard"),
        sa.Column("discount_pct",        sa.Numeric(8, 4), default=0),
        sa.Column("min_margin_pct",      sa.Numeric(8, 4), default=5),
        sa.Column("is_active",           sa.Boolean(),    default=True),
        sa.Column("monthly_order_limit", sa.Integer()),
        sa.Column("notes",               sa.Text()),
        sa.Column("extra_config",        postgresql.JSONB()),
        sa.Column("created_at",          sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at",          sa.DateTime(timezone=True)),
    )

    # ── admins ────────────────────────────────────────────────────────────────
    op.create_table(
        "admins",
        sa.Column("id",              sa.Integer(),  primary_key=True, autoincrement=True),
        sa.Column("username",        sa.String(64), nullable=False, unique=True),
        sa.Column("email",           sa.String(255), nullable=False, unique=True),
        sa.Column("password_hash",   sa.String(255), nullable=False),
        sa.Column("totp_secret",     sa.Text()),
        sa.Column("totp_enabled",    sa.Boolean(), default=False),
        sa.Column("is_active",       sa.Boolean(), default=True),
        sa.Column("role",            sa.String(64), default="admin"),
        sa.Column("failed_attempts", sa.Integer(), default=0),
        sa.Column("locked_until",    sa.DateTime(timezone=True)),
        sa.Column("last_login_at",   sa.DateTime(timezone=True)),
        sa.Column("created_at",      sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at",      sa.DateTime(timezone=True)),
    )

    # ── tenants ───────────────────────────────────────────────────────────────
    op.create_table(
        "tenants",
        sa.Column("id",             sa.Integer(),  primary_key=True, autoincrement=True),
        sa.Column("owner_user_id",  sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("slug",           sa.String(64), nullable=False, unique=True),
        sa.Column("display_name",   sa.String(128), nullable=False),
        sa.Column("bot_token_enc",  sa.Text()),
        sa.Column("bot_username",   sa.String(64)),
        sa.Column("webhook_secret", sa.String(64)),
        sa.Column("plan",           sa.String(32), default="basic"),
        sa.Column("is_active",      sa.Boolean(),  default=False),
        sa.Column("is_deleted",     sa.Boolean(),  default=False),
        sa.Column("config",         postgresql.JSONB()),
        sa.Column("created_at",     sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at",     sa.DateTime(timezone=True)),
    )

    # ── tenant_users ──────────────────────────────────────────────────────────
    op.create_table(
        "tenant_users",
        sa.Column("id",               sa.Integer(),    primary_key=True, autoincrement=True),
        sa.Column("tenant_id",        sa.Integer(),    sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("telegram_id",      sa.BigInteger(), nullable=False),
        sa.Column("first_name",       sa.String(128)),
        sa.Column("username",         sa.String(64)),
        sa.Column("is_active",        sa.Boolean(),    default=True),
        sa.Column("is_banned",        sa.Boolean(),    default=False),
        sa.Column("wallet_balance",   sa.Numeric(18, 4), default=0),
        sa.Column("last_activity_at", sa.DateTime(timezone=True)),
        sa.Column("created_at",       sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "telegram_id"),
    )

    # ── user_premiums ─────────────────────────────────────────────────────────
    op.create_table(
        "user_premiums",
        sa.Column("id",           sa.Integer(),    primary_key=True, autoincrement=True),
        sa.Column("user_id",      sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("plan",         sa.String(32),   nullable=False),
        sa.Column("status",       sa.String(32),   default="active"),
        sa.Column("starts_at",    sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at",   sa.DateTime(timezone=True)),
        sa.Column("payment_ref",  sa.String(128)),
        sa.Column("auto_renew",   sa.Boolean(),    default=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True)),
        sa.Column("expired_at",   sa.DateTime(timezone=True)),
        sa.Column("created_at",   sa.DateTime(timezone=True), nullable=False),
    )

    # ── content_blocks ────────────────────────────────────────────────────────
    op.create_table(
        "content_blocks",
        sa.Column("id",         sa.Integer(),  primary_key=True, autoincrement=True),
        sa.Column("key",        sa.String(64), nullable=False),
        sa.Column("locale",     sa.String(8),  default="en"),
        sa.Column("content",    sa.Text(),     nullable=False),
        sa.Column("is_active",  sa.Boolean(),  default=True),
        sa.Column("version",    sa.Integer(),  default=1),
        sa.Column("updated_by", sa.Integer()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("key", "locale"),
    )

    # ── broadcasts ────────────────────────────────────────────────────────────
    op.create_table(
        "broadcasts",
        sa.Column("id",            sa.Integer(),  primary_key=True, autoincrement=True),
        sa.Column("title",         sa.String(256), nullable=False),
        sa.Column("message",       sa.Text(),      nullable=False),
        sa.Column("segment",       sa.String(32),  default="all"),
        sa.Column("status",        sa.String(32),  default="pending"),
        sa.Column("scheduled_at",  sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at",  sa.DateTime(timezone=True)),
        sa.Column("created_by",    sa.Integer()),
        sa.Column("created_at",    sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_count",    sa.Integer(), default=0),
        sa.Column("failed_count",  sa.Integer(), default=0),
        sa.Column("pending_count", sa.Integer(), default=0),
    )

    # ── audit_log ─────────────────────────────────────────────────────────────
    op.create_table(
        "audit_log",
        sa.Column("id",          sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("actor_id",    sa.BigInteger()),
        sa.Column("action",      sa.String(128),  nullable=False),
        sa.Column("resource",    sa.String(64),   nullable=False),
        sa.Column("resource_id", sa.String(128)),
        sa.Column("details",     postgresql.JSONB()),
        sa.Column("ip_hash",     sa.String(64)),
        sa.Column("tenant_id",   sa.Integer()),
        sa.Column("created_at",  sa.DateTime(timezone=True), nullable=False),
    )

    # ── indexes ───────────────────────────────────────────────────────────────
    op.create_index("ix_orders_user_id",         "orders",               ["user_id"])
    op.create_index("ix_orders_status",          "orders",               ["status"])
    op.create_index("ix_orders_created_at",      "orders",               ["created_at"])
    op.create_index("ix_payments_user_id",       "payments",             ["user_id"])
    op.create_index("ix_wallet_tx_wallet_id",    "wallet_transactions",  ["wallet_id"])
    op.create_index("ix_audit_log_actor_id",     "audit_log",            ["actor_id"])
    op.create_index("ix_audit_log_created_at",   "audit_log",            ["created_at"])
    op.create_index("ix_user_sessions_user_id",  "user_sessions",        ["user_id"])
    op.create_index("ix_user_sessions_expires",  "user_sessions",        ["expires_at"])


def downgrade() -> None:
    op.drop_table("audit_log")
    op.drop_table("broadcasts")
    op.drop_table("content_blocks")
    op.drop_table("user_premiums")
    op.drop_table("tenant_users")
    op.drop_table("tenants")
    op.drop_table("admins")
    op.drop_table("reseller_profiles")
    op.drop_table("coupon_redemptions")
    op.drop_table("coupons")
    op.drop_table("user_sessions")
    op.drop_table("payments")
    op.drop_table("orders")
    op.drop_table("pricing_rules")
    op.drop_table("service_provider_mappings")
    op.drop_table("services")
    op.drop_table("categories")
    op.drop_table("provider_services")
    op.drop_table("providers")
    op.drop_table("wallet_transactions")
    op.drop_table("wallets")
    op.drop_table("users")
