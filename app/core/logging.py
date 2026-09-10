"""Structlog JSON logging with secret scrubbing and request-id context."""
from __future__ import annotations
import contextvars
import logging
import structlog

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")
user_id_var: contextvars.ContextVar[int]    = contextvars.ContextVar("user_id",    default=0)

_SCRUB_KEYS = frozenset({
    "password", "token", "secret", "api_key", "bot_token",
    "credentials_enc", "bot_token_enc", "webhook_secret",
    "razorpay_key_secret", "stripe_secret_key", "encryption_key",
    "authorization", "cookie",
})


def _scrub_secrets(logger, method, event_dict: dict) -> dict:
    """Remove sensitive keys from structlog event dicts before writing."""
    for key in list(event_dict.keys()):
        if key.lower() in _SCRUB_KEYS:
            event_dict[key] = "***REDACTED***"
    return event_dict


def _add_context(logger, method, event_dict: dict) -> dict:
    rid = request_id_var.get("")
    uid = user_id_var.get(0)
    if rid: event_dict["request_id"] = rid
    if uid: event_dict["user_id"]    = uid
    return event_dict


def new_request_id() -> str:
    import uuid
    rid = str(uuid.uuid4())[:8]
    request_id_var.set(rid)
    return rid


def configure_logging(level: str = "INFO") -> None:
    structlog.configure(
        processors=[
            _add_context,
            _scrub_secrets,
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
    )
    logging.basicConfig(
        format="%(message)s",
        level=getattr(logging, level.upper(), logging.INFO),
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name or __name__)
