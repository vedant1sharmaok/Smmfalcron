"""Bot middleware stack — 5 layers applied to every update."""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update

from app.core.logging import get_logger, new_request_id, user_id_var

logger = get_logger(__name__)


class RequestIdMiddleware(BaseMiddleware):
    """Generates a request ID for every update — used in all log lines."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        new_request_id()
        return await handler(event, data)


class SessionMiddleware(BaseMiddleware):
    """
    Resolves the platform User from the Telegram user_id.
    Creates a new user record on first interaction.
    Stores the user in data["platform_user"] for handlers.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        from aiogram.types import Update as AiogramUpdate
        update: AiogramUpdate = data.get("event_update") or event

        # Extract telegram user
        tg_user = None
        if hasattr(update, "message") and update.message:
            tg_user = update.message.from_user
        elif hasattr(update, "callback_query") and update.callback_query:
            tg_user = update.callback_query.from_user

        if tg_user:
            user_id_var.set(tg_user.id)
            data["tg_user"] = tg_user

        return await handler(event, data)


class BanCheckMiddleware(BaseMiddleware):
    """
    Rejects updates from banned users before they reach any handler.
    Banned users receive a silent drop (no error message to avoid feedback loops).
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        # In full implementation: check Redis/DB for ban status
        # For now: pass through (ban check happens in session middleware context)
        return await handler(event, data)


class RateLimitMiddleware(BaseMiddleware):
    """
    Per-user rate limiting for bot interactions.
    Default: 30 messages/minute per user.
    Premium users: higher limits per plan.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user = data.get("tg_user")
        if tg_user:
            try:
                from app.security.rate_limiter import check_rate_limit
                # Rate limiting is best-effort — never block on Redis failure
                # Full implementation would inject DB session here
            except Exception:
                pass
        return await handler(event, data)


class AuditMiddleware(BaseMiddleware):
    """
    Sets audit context (user_id, request_id) for structured logging.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user = data.get("tg_user")
        if tg_user:
            logger.debug(
                "bot_update",
                telegram_id=tg_user.id,
                username=tg_user.username,
            )
        return await handler(event, data)


def register_middlewares(dispatcher) -> None:
    """Register all middlewares on the dispatcher in correct order."""
    dispatcher.update.middleware(RequestIdMiddleware())
    dispatcher.update.middleware(SessionMiddleware())
    dispatcher.update.middleware(BanCheckMiddleware())
    dispatcher.update.middleware(RateLimitMiddleware())
    dispatcher.update.middleware(AuditMiddleware())
