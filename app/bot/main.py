"""Aiogram dispatcher, session middleware, polling entry."""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, ErrorEvent, Message, TelegramObject, Update
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.handlers import setup_routers
from app.config import Settings
from app.db import get_session_factory
from app.ratelimit import RateLimiter

log = logging.getLogger("falaron.bot")

_TERMS_FREE_CALLBACKS = {"m:accept_terms", "m:terms", "m:privacy"}


class ThrottleMiddleware(BaseMiddleware):
    """Per-user flood control before any DB work. Silent drop: spam gets no amplification."""

    def __init__(self, per_minute: int = 45) -> None:
        super().__init__()
        self._limiter = RateLimiter(per_minute, 60.0)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user = data.get("event_from_user")
        if tg_user is not None and not self._limiter.allow(str(tg_user.id)):
            if isinstance(event, Update) and event.callback_query is not None:
                try:
                    await event.callback_query.answer("Too many requests — slow down a little.")
                except Exception:  # noqa: BLE001
                    pass
            return None
        return await handler(event, data)


class DbSessionMiddleware(BaseMiddleware):
    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self._settings = settings
        self._factory = get_session_factory(settings)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        async with self._factory() as session:
            data["session"] = session
            data["settings"] = self._settings
            try:
                result = await handler(event, data)
                await session.commit()
                return result
            except Exception:
                await session.rollback()
                raise


class AccessMiddleware(BaseMiddleware):
    """Block banned accounts and force the terms gate before any other screen."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        from app.bot.menus import terms_kb
        from app.models import User as DbUser

        tg_user = data.get("event_from_user")
        session: AsyncSession | None = data.get("session")
        if tg_user is None or session is None:
            return await handler(event, data)

        row = await session.get(DbUser, tg_user.id)
        callback: CallbackQuery | None = None
        message: Message | None = None
        if isinstance(event, Update):
            callback = event.callback_query
            message = event.message or (event.edited_message if hasattr(event, "edited_message") else None)
        elif isinstance(event, CallbackQuery):
            callback = event
        elif isinstance(event, Message):
            message = event

        if row is not None and row.is_banned:
            text = "This account is suspended. Contact support if you believe this is a mistake."
            if callback is not None:
                try:
                    await callback.answer(text, show_alert=True)
                except Exception:  # noqa: BLE001
                    pass
            elif message is not None:
                await message.answer(text)
            return None

        data_raw = (callback.data if callback is not None else "") or ""
        is_start = bool(
            message is not None
            and message.text
            and message.text.startswith("/start")
        )
        if (
            row is not None
            and row.terms_accepted_at is None
            and not is_start
            and data_raw not in _TERMS_FREE_CALLBACKS
            and not data_raw.startswith("m:accept_terms")
        ):
            from app.bot.handlers.start import terms_text

            text = await terms_text(session)
            if callback is not None and isinstance(callback.message, Message):
                try:
                    await callback.answer()
                    await callback.message.edit_text(text, reply_markup=terms_kb(), parse_mode="HTML")
                except Exception:  # noqa: BLE001
                    await callback.message.answer(text, reply_markup=terms_kb(), parse_mode="HTML")
            elif message is not None:
                await message.answer(text, reply_markup=terms_kb(), parse_mode="HTML")
            return None

        return await handler(event, data)


def create_bot(settings: Settings) -> Bot:
    return Bot(
        token=settings.bot_token_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


def create_dispatcher(settings: Settings) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(ThrottleMiddleware())
    dp.update.outer_middleware(DbSessionMiddleware(settings))
    dp.update.middleware(AccessMiddleware())
    dp.include_router(setup_routers())

    @dp.errors()
    async def on_error(event: ErrorEvent) -> None:
        log.exception("unhandled bot error: %s", event.exception)

    return dp
