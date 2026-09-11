"""
Application settings loaded from environment variables.

On Render: set environment variables in the Render dashboard.
Locally: use a .env file in the project root.

IMPORTANT: DATABASE_URL must be set as a Render environment variable.
Do NOT rely on .env in production — Render does not copy .env into containers.
"""
from __future__ import annotations

import os
from decimal import Decimal
from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ── Database ──────────────────────────────────────────────────────────────
    # Default to localhost for local dev only.
    # In production (Render) these must be set as env vars in the dashboard.
    database_url: str = "postgresql+asyncpg://smm_app:dev@localhost:5432/smm_platform"
    database_url_sync: str = "postgresql://smm_app:dev@localhost:5432/smm_platform"
    db_pool_size: int = 5
    db_max_overflow: int = 10

    # ── Redis ─────────────────────────────────────────────────────────────────
    redis_url: str = "redis://localhost:6379/0"
    redis_worker_url: str = "redis://localhost:6379/1"
    redis_pool_size: int = 20

    # ── Encryption ────────────────────────────────────────────────────────────
    encryption_key: str = "a" * 64
    config_encryption_key: str = "b" * 64
    backup_encryption_key: str = "c" * 64

    # ── Bot ───────────────────────────────────────────────────────────────────
    bot_token: str = ""
    bot_webhook_secret: str = ""
    bot_webhook_url: str = ""
    telegram_init_data_max_age: int = 300

    # ── Admin JWT ─────────────────────────────────────────────────────────────
    admin_jwt_secret: str = "change-me-in-prod-must-be-long-random"

    # ── Razorpay ──────────────────────────────────────────────────────────────
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_webhook_secret: str = ""

    # ── Stripe ────────────────────────────────────────────────────────────────
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_currency: str = "inr"

    # ── Backup / S3 ───────────────────────────────────────────────────────────
    backup_s3_bucket: str = ""
    backup_s3_endpoint: str = ""
    backup_s3_access_key: str = ""
    backup_s3_secret_key: str = ""
    backup_retention_daily: int = 30

    # ── FX ────────────────────────────────────────────────────────────────────
    usd_to_inr_rate: Decimal = Decimal("83.00")
    fixer_api_key: str = ""

    # ── Monitoring ────────────────────────────────────────────────────────────
    metrics_bearer_token: str = ""
    sentry_dsn: str = ""
    log_level: str = "INFO"
    log_ch_critical: int = 0

    # ── App ───────────────────────────────────────────────────────────────────
    app_env: str = "development"
    domain: str = "localhost"
    app_workers: int = 1

    # ── Workers ───────────────────────────────────────────────────────────────
    worker_order_monitor_interval: int = 120
    worker_payment_reconciler_interval: int = 900

    model_config = {
        # Load .env for local development.
        # On Render this file doesn't exist — that's fine, env vars come
        # from the Render dashboard and are already in os.environ.
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "case_sensitive": False,
        # Never fail if .env is missing.
        "extra": "ignore",
    }


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
