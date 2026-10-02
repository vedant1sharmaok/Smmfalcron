"""Application settings. Secrets never appear in logs or Telegram messages.

`APP_ENV=production` turns on strict startup validation: the process refuses to
boot with demo payments, a derived encryption key, SQLite, or missing gateway
credentials. Development keeps the friendly defaults.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Literal
from urllib.parse import urlparse

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- core -----------------------------------------------------------
    app_env: Literal["development", "production"] = "development"
    bot_token: SecretStr
    owner_telegram_id: int
    database_url: str = "sqlite+aiosqlite:///./falaron.db"
    public_base_url: str | None = None  # e.g. https://shop.example.com (no trailing slash)
    webapp_url: str | None = None  # defaults to PUBLIC_BASE_URL + /app/
    bot_username: str | None = None
    brand_name: str = "FALARON"
    support_url: str | None = None  # e.g. https://t.me/your_support
    terms_url: str | None = None
    privacy_url: str | None = None
    secret_key: SecretStr | None = None
    api_host: str = "0.0.0.0"
    api_port: int = 8080
    bot_polling: bool = True
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    cors_origins: str = ""
    trust_proxy: bool = False  # honour X-Forwarded-For (set true behind Caddy)

    # ---- providers ------------------------------------------------------
    smm_api_url: str | None = None
    smm_api_key: SecretStr | None = None
    provider_sync_interval_seconds: int = 3600
    allow_private_provider_urls: bool = False  # SSRF guard; dev only

    # ---- payments -------------------------------------------------------
    payment_gateway: Literal["mock", "razorpay", "manual"] = "mock"
    enable_mock_payments: bool = True
    razorpay_key_id: str | None = None
    razorpay_key_secret: SecretStr | None = None
    razorpay_webhook_secret: SecretStr | None = None
    razorpay_base_url: str = "https://api.razorpay.com/v1"
    min_deposit_paise: int = 10_000  # INR 100.00
    max_deposit_paise: int = 5_000_000  # INR 50,000.00
    payment_reconcile_interval_seconds: int = 300

    # ---- abuse controls -------------------------------------------------
    rate_limit_per_minute: int = 90  # per authenticated user
    ip_rate_limit_per_minute: int = 240  # per client IP (all /api routes)
    order_rate_limit_per_minute: int = 12  # per user, order placement only

    # ---- business defaults (overridable via app_settings table after seed) ----
    welcome_bonus_paise: int = 25_000  # INR 250.00 (set 0 in production)
    global_markup_percent: float = 25.0
    min_margin_percent: float = 5.0
    currency_code: str = "INR"
    currency_symbol: str = "₹"

    @field_validator(
        "webapp_url",
        "smm_api_url",
        "bot_username",
        "public_base_url",
        "razorpay_key_id",
        "support_url",
        "terms_url",
        "privacy_url",
        mode="before",
    )
    @classmethod
    def empty_str_to_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("bot_token")
    @classmethod
    def bot_token_shape(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value().strip()
        if ":" not in raw or len(raw) < 20:
            raise ValueError("BOT_TOKEN is missing or malformed (expected 123456:ABC... from @BotFather)")
        return SecretStr(raw)

    @field_validator("public_base_url")
    @classmethod
    def strip_trailing_slash(cls, value: str | None) -> str | None:
        return value.rstrip("/") if value else value

    @model_validator(mode="after")
    def default_webapp_url(self) -> "Settings":
        if not self.webapp_url and self.public_base_url:
            self.webapp_url = f"{self.public_base_url}/app/"
        # Free spendable credit for every new Telegram account is a demo feature: off by default in
        # production unless the operator set it explicitly.
        if self.is_production and "welcome_bonus_paise" not in self.model_fields_set:
            self.welcome_bonus_paise = 0
        return self

    # ---- derived --------------------------------------------------------
    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def has_real_smm_panel(self) -> bool:
        key = self.smm_api_key.get_secret_value() if self.smm_api_key else ""
        return bool(self.smm_api_url and key)

    @property
    def mock_payments_allowed(self) -> bool:
        """Mock invoices may only ever exist outside production."""
        return (
            not self.is_production
            and self.enable_mock_payments
            and self.payment_gateway == "mock"
        )

    @property
    def cors_origin_list(self) -> list[str]:
        raw = [part.strip() for part in self.cors_origins.split(",") if part.strip()]
        if self.public_base_url:
            origin = self.public_base_url.rstrip("/")
            if origin not in raw:
                raw.append(origin)
        return raw or ["*"]

    def bot_token_value(self) -> str:
        return self.bot_token.get_secret_value()

    def smm_api_key_value(self) -> str | None:
        if not self.smm_api_key:
            return None
        value = self.smm_api_key.get_secret_value().strip()
        return value or None

    def razorpay_secret_value(self) -> str | None:
        if not self.razorpay_key_secret:
            return None
        return self.razorpay_key_secret.get_secret_value().strip() or None

    def razorpay_webhook_secret_value(self) -> str | None:
        if not self.razorpay_webhook_secret:
            return None
        return self.razorpay_webhook_secret.get_secret_value().strip() or None

    # ---- production validation -----------------------------------------
    def production_problems(self) -> list[str]:
        """Blocking problems. Empty list means safe to boot in production."""
        problems: list[str] = []
        if not self.is_production:
            return problems

        key = self.secret_key.get_secret_value().strip() if self.secret_key else ""
        if not key:
            problems.append(
                "SECRET_KEY is not set. Generate one with: "
                'python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
            )
        elif len(key) < 32:
            problems.append("SECRET_KEY is too short (need a 44-char Fernet key or 32+ random chars).")

        if self.is_sqlite:
            problems.append("DATABASE_URL must be PostgreSQL in production (postgresql+asyncpg://...).")

        if not self.public_base_url or not self.public_base_url.startswith("https://"):
            problems.append("PUBLIC_BASE_URL must be set and start with https:// (Telegram Mini Apps require HTTPS).")

        if self.payment_gateway == "mock" or self.enable_mock_payments:
            problems.append(
                "Mock payments must be off in production: set ENABLE_MOCK_PAYMENTS=false and "
                "PAYMENT_GATEWAY=razorpay (or manual)."
            )

        if self.payment_gateway == "razorpay":
            if not self.razorpay_key_id:
                problems.append("RAZORPAY_KEY_ID is required when PAYMENT_GATEWAY=razorpay.")
            if not self.razorpay_secret_value():
                problems.append("RAZORPAY_KEY_SECRET is required when PAYMENT_GATEWAY=razorpay.")
            if not self.razorpay_webhook_secret_value():
                problems.append("RAZORPAY_WEBHOOK_SECRET is required when PAYMENT_GATEWAY=razorpay.")

        if self.cors_origin_list == ["*"]:
            problems.append("CORS is wide open; set PUBLIC_BASE_URL or CORS_ORIGINS.")

        if not self.terms_url or not self.privacy_url:
            problems.append("TERMS_URL and PRIVACY_URL must be set (users must accept real documents).")

        if not self.bot_polling:
            problems.append(
                "BOT_POLLING=false leaves the bot without an update source (webhook mode is not built in)."
            )
        return problems

    def production_warnings(self) -> list[str]:
        warnings: list[str] = []
        if not self.is_production:
            return warnings
        if self.welcome_bonus_paise > 0:
            warnings.append(
                "WELCOME_BONUS_PAISE is > 0: every new Telegram account receives spendable credit "
                "(abusable with throw-away accounts). Set it to 0 unless this is intentional."
            )
        if self.payment_gateway == "manual":
            warnings.append("PAYMENT_GATEWAY=manual: deposits are credited by an admin, not automatically.")
        if not self.trust_proxy:
            warnings.append("TRUST_PROXY=false: rate limits will key on the proxy IP when behind Caddy.")
        if not self.has_real_smm_panel:
            warnings.append(
                "No SMM_API_URL/SMM_API_KEY set: add a provider with "
                "`python -m app.cli add-provider` before taking orders."
            )
        if self.public_base_url:
            host = urlparse(self.public_base_url).hostname or ""
            if host in {"localhost", "127.0.0.1"}:
                warnings.append("PUBLIC_BASE_URL points at localhost.")
        return warnings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Keep third-party noise down; never attach token filters that print env.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
