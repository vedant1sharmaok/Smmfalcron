"""FastAPI dependency that validates Telegram Mini App initData."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.limits import check_ip, check_user
from app.config import Settings, get_settings
from app.db import get_session_factory
from app.models import User
from app.security import extract_telegram_user_id, validate_telegram_init_data
from app.wallet import get_or_create_user


async def get_db() -> AsyncIterator[AsyncSession]:
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


DbDep = Annotated[AsyncSession, Depends(get_db)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


async def current_user(
    request: Request,
    session: DbDep,
    settings: SettingsDep,
    x_telegram_init_data: Annotated[str | None, Header()] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> User:
    check_ip(request)  # before any crypto or DB work
    init_data = x_telegram_init_data
    if not init_data and authorization and authorization.lower().startswith("tma "):
        init_data = authorization[4:]
    if not init_data:
        raise HTTPException(status_code=401, detail="Missing Telegram initData")
    try:
        fields = validate_telegram_init_data(init_data, settings.bot_token_value())
        telegram_id = extract_telegram_user_id(fields)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Invalid Telegram initData") from exc
    tg_user: dict[str, Any] = fields.get("user") or {}
    user = await get_or_create_user(
        session,
        telegram_id,
        username=tg_user.get("username"),
        first_name=tg_user.get("first_name"),
        last_name=tg_user.get("last_name"),
        language_code=tg_user.get("language_code"),
        owner_id=settings.owner_telegram_id,
    )
    if user.is_banned:
        raise HTTPException(status_code=403, detail="Account suspended")
    check_user(user.telegram_id)
    return user


UserDep = Annotated[User, Depends(current_user)]
