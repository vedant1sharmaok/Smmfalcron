"""Add missing tables — service_versions, order_events, refill_orders,
payment_events, refunds, promotions, referrals, notifications,
admin_actions, security_events, provider_health, sync_runs,
system_settings, feature_flags.

Revision ID: 0002
Revises: 0001
Create Date: 2024-08-14 00:00:00
"""
from __future__ import annotations
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision      = "0002"
down_revision = "0001"
branch_labels = None
depends_on    = None


def upgrade() -> None:
    # ── service_versions (Section 35) ─────────────────────────────────────────
    op.create_table(
        "service_versions",
        sa.Column("id",              sa.Integer(),    primary_key=True, autoincrement=True),
        sa.Column("service_id",      sa.Integer(),    sa.ForeignKey("services.id"), nullable=False),
        sa.Column("version",         sa.Integer(),    nullable=False, default=1),
        sa.Column("display_name",    sa.String(512)),
        sa.Column("custom_price",    sa.Numeric(18, 6)),
        sa.Column("profit_pct",      sa.Numeric(8, 4)),
        sa.Column("min_qty",         sa.Integer()),
        sa.Column("max_qty",         sa.Integer()),
        sa.Column("snapshot",        postgresql.JSONB()),
        sa.Column("created_at",      sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_service_versions_service_id", "service_versions", ["service_id"])

    # ── order_events (Section 14) ─────────────────────────────────────────────
    op.create_table(
        "order_events",
        sa.Column("id",           sa.BigInteger(),  primary_key=True, autoincrement=True),
        sa.Column("order_id",     sa.String(36),    sa.ForeignKey("orders.id"), nullable=False),
        sa.Column("event_type",   sa.String(64),    nullable=False),
        sa.Column("from_status",  sa.String(32)),
        sa.Column("to_status",    sa.String(32)),
        sa.Column("details",      postgresql.JSONB()),
        sa.Column("actor_id",     sa.BigInteger()),
        sa.Column("created_at",   sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_order_events_order_id", "order_events", ["order_id"])

    # ── refill_orders (Section 15) ────────────────────────────────────────────
    op.create_table(
        "refill_orders",
        sa.Column("id",                  sa.Integer(),    primary_key=True, autoincrement=True),
        sa.Column("order_id",            sa.String(36),   sa.ForeignKey("orders.id"), nullable=False),
        sa.Column("user_id",             sa.BigInteger(), sa.ForeignKey("users.id"),  nullable=False),
        sa.Column("provider_refill_id",  sa.String(128)),
        sa.Column("status",              sa.String(32),   default="pending"),
        sa.Column("idempotency_key",     sa.String(128),  unique=True),
        sa.Column("created_at",          sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at",          sa.DateTime(timezone=True)),
    )
    op.create_index("ix_refill_orders_order_id", "refill_orders", ["order_id"])

    # ── payment_events ────────────────────────────────────────────────────────
    op.create_table(
        "payment_events",
        sa.Column("id",           sa.BigInteger(),  primary_key=True, autoincrement=True),
        sa.Column("payment_id",   sa.Integer(),     sa.ForeignKey("payments.id"), nullable=False),
        sa.Column("event_type",   sa.String(64),    nullable=False),
        sa.Column("details",      postgresql.JSONB()),
        sa.Column("created_at",   sa.DateTime(timezone=True), nullable=False),
    )

    # ── refunds ───────────────────────────────────────────────────────────────
    op.create_table(
        "refunds",
        sa.Column("id",             sa.Integer(),    primary_key=True, autoincrement=True),
        sa.Column("order_id",       sa.String(36),   sa.ForeignKey("orders.id")),
        sa.Column("payment_id",     sa.Integer(),    sa.ForeignKey("payments.id")),
        sa.Column("user_id",        sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("amount",         sa.Numeric(18, 4), nullable=False),
        sa.Column("reason",         sa.Text()),
        sa.Column("actor_id",       sa.BigInteger()),
        sa.Column("idempotency_key",sa.String(128),  unique=True),
        sa.Column("created_at",     sa.DateTime(timezone=True), nullable=False),
    )

    # ── promotions ────────────────────────────────────────────────────────────
    op.create_table(
        "promotions",
        sa.Column("id",           sa.Integer(),   primary_key=True, autoincrement=True),
        sa.Column("name",         sa.String(128), nullable=False),
        sa.Column("promo_type",   sa.String(32),  nullable=False),
        sa.Column("discount_pct", sa.Numeric(8, 4)),
        sa.Column("flat_amount",  sa.Numeric(18, 4)),
        sa.Column("conditions",   postgresql.JSONB()),
        sa.Column("starts_at",    sa.DateTime(timezone=True)),
        sa.Column("expires_at",   sa.DateTime(timezone=True)),
        sa.Column("is_active",    sa.Boolean(), default=True),
        sa.Column("created_at",   sa.DateTime(timezone=True), nullable=False),
    )

    # ── referrals ─────────────────────────────────────────────────────────────
    op.create_table(
        "referrals",
        sa.Column("id",             sa.Integer(),    primary_key=True, autoincrement=True),
        sa.Column("referrer_id",    sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("referred_id",    sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False, unique=True),
        sa.Column("reward_amount",  sa.Numeric(18, 4), default=10),
        sa.Column("reward_paid",    sa.Boolean(), default=False),
        sa.Column("created_at",     sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_referrals_referrer_id", "referrals", ["referrer_id"])

    # ── notifications ─────────────────────────────────────────────────────────
    op.create_table(
        "notifications",
        sa.Column("id",           sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id",      sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("notif_type",   sa.String(64),   nullable=False),
        sa.Column("title",        sa.String(256)),
        sa.Column("body",         sa.Text()),
        sa.Column("reference_id", sa.String(128)),
        sa.Column("is_sent",      sa.Boolean(), default=False),
        sa.Column("sent_at",      sa.DateTime(timezone=True)),
        sa.Column("created_at",   sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_notifications_user_id", "notifications", ["user_id"])

    # ── admin_actions ─────────────────────────────────────────────────────────
    op.create_table(
        "admin_actions",
        sa.Column("id",          sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("admin_id",    sa.Integer(),    sa.ForeignKey("admins.id")),
        sa.Column("action",      sa.String(128),  nullable=False),
        sa.Column("resource",    sa.String(64),   nullable=False),
        sa.Column("resource_id", sa.String(128)),
        sa.Column("old_value",   postgresql.JSONB()),
        sa.Column("new_value",   postgresql.JSONB()),
        sa.Column("reason",      sa.Text()),
        sa.Column("ip_hash",     sa.String(64)),
        sa.Column("created_at",  sa.DateTime(timezone=True), nullable=False),
    )

    # ── security_events (Section 24, 29) ──────────────────────────────────────
    op.create_table(
        "security_events",
        sa.Column("id",           sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("event_type",   sa.String(64),   nullable=False),
        sa.Column("severity",     sa.String(16),   default="medium"),
        sa.Column("user_id",      sa.BigInteger()),
        sa.Column("ip_hash",      sa.String(64)),
        sa.Column("details",      postgresql.JSONB()),
        sa.Column("resolved",     sa.Boolean(), default=False),
        sa.Column("resolved_at",  sa.DateTime(timezone=True)),
        sa.Column("created_at",   sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_security_events_event_type", "security_events", ["event_type"])
    op.create_index("ix_security_events_created_at", "security_events", ["created_at"])

    # ── provider_health (Section 30) ──────────────────────────────────────────
    op.create_table(
        "provider_health",
        sa.Column("id",              sa.Integer(),    primary_key=True, autoincrement=True),
        sa.Column("provider_id",     sa.Integer(),    sa.ForeignKey("providers.id"), nullable=False),
        sa.Column("latency_ms",      sa.Integer()),
        sa.Column("success_rate",    sa.Numeric(5, 2)),
        sa.Column("error_rate",      sa.Numeric(5, 2)),
        sa.Column("balance",         sa.Numeric(18, 4)),
        sa.Column("status",          sa.String(32), default="unknown"),
        sa.Column("checked_at",      sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_provider_health_provider_id", "provider_health", ["provider_id"])
    op.create_index("ix_provider_health_checked_at",  "provider_health", ["checked_at"])

    # ── sync_runs ─────────────────────────────────────────────────────────────
    op.create_table(
        "sync_runs",
        sa.Column("id",           sa.Integer(),    primary_key=True, autoincrement=True),
        sa.Column("provider_id",  sa.Integer(),    sa.ForeignKey("providers.id"), nullable=False),
        sa.Column("status",       sa.String(32),   default="running"),
        sa.Column("inserted",     sa.Integer(),    default=0),
        sa.Column("updated",      sa.Integer(),    default=0),
        sa.Column("deactivated",  sa.Integer(),    default=0),
        sa.Column("errors",       sa.Integer(),    default=0),
        sa.Column("started_at",   sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("error_detail", sa.Text()),
    )

    # ── system_settings ───────────────────────────────────────────────────────
    op.create_table(
        "system_settings",
        sa.Column("key",         sa.String(128),  primary_key=True),
        sa.Column("value",       sa.Text(),       nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("updated_by",  sa.Integer()),
        sa.Column("updated_at",  sa.DateTime(timezone=True)),
    )

    # ── feature_flags ─────────────────────────────────────────────────────────
    op.create_table(
        "feature_flags",
        sa.Column("name",        sa.String(128),  primary_key=True),
        sa.Column("enabled",     sa.Boolean(),    default=False),
        sa.Column("description", sa.Text()),
        sa.Column("updated_by",  sa.Integer()),
        sa.Column("updated_at",  sa.DateTime(timezone=True)),
    )

    # ── Insert default system settings ────────────────────────────────────────
    op.bulk_insert(
        sa.table("system_settings",
            sa.column("key", sa.String),
            sa.column("value", sa.Text),
            sa.column("description", sa.Text),
        ),
        [
            {"key": "platform_name",          "value": "SMM Falcron",  "description": "Platform display name"},
            {"key": "support_username",        "value": "",             "description": "Support @username"},
            {"key": "min_deposit_inr",         "value": "10",           "description": "Minimum deposit amount in INR"},
            {"key": "max_deposit_inr",         "value": "100000",       "description": "Maximum deposit amount in INR"},
            {"key": "referral_reward_inr",     "value": "10",           "description": "Referral reward in INR"},
            {"key": "policy_version",          "value": "1",            "description": "Current policy version"},
            {"key": "order_monitor_interval",  "value": "120",          "description": "Order monitor interval in seconds"},
        ],
    )

    # ── Insert default feature flags ──────────────────────────────────────────
    op.bulk_insert(
        sa.table("feature_flags",
            sa.column("name", sa.String),
            sa.column("enabled", sa.Boolean),
            sa.column("description", sa.Text),
        ),
        [
            {"name": "referral_system",    "enabled": False, "description": "Enable referral rewards"},
            {"name": "premium_plans",      "enabled": True,  "description": "Enable premium plan upgrades"},
            {"name": "coupon_system",      "enabled": True,  "description": "Enable coupon codes"},
            {"name": "reseller_api",       "enabled": False, "description": "Enable reseller API access"},
            {"name": "hosted_bots",        "enabled": False, "description": "Enable hosted bot multi-tenancy"},
            {"name": "custom_order_forms", "enabled": True,  "description": "Enable dynamic order form types"},
        ],
    )


def downgrade() -> None:
    op.drop_table("feature_flags")
    op.drop_table("system_settings")
    op.drop_table("sync_runs")
    op.drop_table("provider_health")
    op.drop_table("security_events")
    op.drop_table("admin_actions")
    op.drop_table("notifications")
    op.drop_table("referrals")
    op.drop_table("promotions")
    op.drop_table("refunds")
    op.drop_table("payment_events")
    op.drop_table("refill_orders")
    op.drop_table("order_events")
    op.drop_table("service_versions")
