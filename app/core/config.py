"""
Application settings — loaded from environment variables.

CRITICAL: lru_cache is intentionally NOT used here.
On Render, DATABASE_URL is injected as an environment variable at runtime.
If Settings() is cached at module import time (before Render populates os.environ),
it reads the localhost default and every DB connection fails with:
  OSError: [Errno 111] Connect call failed ('127.0.0.1', 5432)

Solution: call Settings() fresh on each get_settings() call so it always
reads the current os.environ. pydantic-settings re-reads env vars on
every instantiation, so this is safe and cheap.
"""
from __future__ import annotations

import os
from decimal import Decimal

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ── Database ──────────────────────────────────────────────────────────────
    # Defaults are localhost — only for local dev.
    # On Render: set DATABASE_URL in the Render environment variables panel.
    database_url: str
    database_url_sync: str
    db_pool_size: int      = 5
    db_max_overflow: int   = 10

    # ── Redis ─────────────────────────────────────────────────────────────────
    redis_url: str        = "redis://localhost:6379/0"
    redis_worker_url: str = "redis://localhost:6379/1"
    redis_pool_size: int  = 20

    # ── Encryption ────────────────────────────────────────────────────────────
    encryption_key:        str = "a" * 64
    config_encryption_key: str = "b" * 64
    backup_encryption_key: str = "c" * 64

    # ── Bot ───────────────────────────────────────────────────────────────────
    bot_token:                  str = ""
    bot_webhook_secret:         str = ""
    bot_webhook_url:            str = ""
    telegram_init_data_max_age: int = 300

    # ── Admin JWT ─────────────────────────────────────────────────────────────
    admin_jwt_secret: str = "change-me-in-prod-must-be-long-random"

    # ── Razorpay ──────────────────────────────────────────────────────────────
    razorpay_key_id:        str = ""
    razorpay_key_secret:    str = ""
    razorpay_webhook_secret: str = ""

    # ── Stripe ────────────────────────────────────────────────────────────────
    stripe_secret_key:    str = ""
    stripe_webhook_secret: str = ""
    stripe_currency:       str = "inr"

    # ── Backup / S3 ───────────────────────────────────────────────────────────
    backup_s3_bucket:       str = ""
    backup_s3_endpoint:     str = ""
    backup_s3_access_key:   str = ""
    backup_s3_secret_key:   str = ""
    backup_retention_daily: int = 30

    # ── FX ────────────────────────────────────────────────────────────────────
    usd_to_inr_rate: Decimal = Decimal("83.00")
    fixer_api_key:   str     = ""

    # ── Monitoring ────────────────────────────────────────────────────────────
    metrics_bearer_token: str = ""
    sentry_dsn:           str = ""
    log_level:            str = "INFO"
    log_ch_critical:      int = 0

    # ── App ───────────────────────────────────────────────────────────────────
    app_env:     str = "development"
    domain:      str = "localhost"
    app_workers: int = 1

    # ── Workers ───────────────────────────────────────────────────────────────
    worker_order_monitor_interval:     int = 120
    worker_payment_reconciler_interval:int = 900

    model_config = {
        # Load .env for local development.
        # On Render, .env is NOT in the container — env vars come from the
        # Render dashboard. env_file missing is silently ignored.
        "env_file":          ".env",
        "env_file_encoding": "utf-8",
        "case_sensitive":    False,
        "extra":             "ignore",
    }


def get_settings() -> Settings:
    """
    Return a fresh Settings() instance every call.
    NO lru_cache — ensures Render's runtime env vars are always read.
    """
    return Settings()


# Module-level singleton — created once when this module is first imported.
# On Render this import happens AFTER the platform has injected env vars,
# so this reads the correct DATABASE_URL from the environment.
settings = Settings()
