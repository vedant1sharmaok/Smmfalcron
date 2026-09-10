"""
Application settings loaded from environment variables.
Uses pydantic-settings for validation and type coercion.
"""
from __future__ import annotations
from decimal import Decimal
from functools import lru_cache
from typing import Any
from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ── Database ──────────────────────────────────────────────────────────────
    database_url: str = "postgresql+asyncpg://smm_app:dev@localhost:5432/smm_platform"
    database_url_sync: str = "postgresql://smm_app:dev@localhost:5432/smm_platform"
    db_pool_size: int = 10
    db_max_overflow: int = 20

    # ── Redis ─────────────────────────────────────────────────────────────────
    redis_url: str = "redis://:password@localhost:6379/0"
    redis_worker_url: str = "redis://:password@localhost:6379/1"
    redis_pool_size: int = 20

    # ── Encryption ────────────────────────────────────────────────────────────
    encryption_key: str = "a" * 64          # 64 hex chars = 32 bytes AES key
    config_encryption_key: str = "b" * 64
    backup_encryption_key: str = "c" * 64

    # ── Bot ───────────────────────────────────────────────────────────────────
    bot_token: str = ""
    bot_webhook_secret: str = ""
    bot_webhook_url: str = ""
    telegram_init_data_max_age: int = 300   # seconds

    # ── Admin JWT ─────────────────────────────────────────────────────────────
    admin_jwt_secret: str = "change-me-in-prod-admin-jwt-secret"

    # ── Razorpay ─────────────────────────────────────────────────────────────
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_webhook_secret: str = ""

    # ── Stripe ───────────────────────────────────────────────────────────────
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
    usd_to_inr_rate: Decimal = Decimal("83.00")   # fallback; live rate from Redis
    fixer_api_key: str = ""

    # ── Monitoring ────────────────────────────────────────────────────────────
    metrics_bearer_token: str = ""
    sentry_dsn: str = ""
    log_level: str = "INFO"
    log_ch_critical: int = 0   # Telegram channel ID for critical alerts

    # ── App ───────────────────────────────────────────────────────────────────
    app_env: str = "development"
    domain: str = "localhost"
    app_workers: int = 4

    # ── Workers ───────────────────────────────────────────────────────────────
    worker_order_monitor_interval: int = 120   # seconds
    worker_payment_reconciler_interval: int = 900

    model_config = {"env_file": ".env", "case_sensitive": False}


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
