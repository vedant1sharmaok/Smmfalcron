"""Custom ad placements shown above bot screens."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AD_PLACEMENTS, Ad

# Imported lazily in ad_block to avoid circular menus import at module load.


async def fetch_ad(session: AsyncSession, placement: str) -> Ad | None:
    placement = (placement or "").strip().lower()
    if placement not in AD_PLACEMENTS:
        placement = "home"
    row = (
        await session.execute(
            select(Ad)
            .where(Ad.placement == placement, Ad.active.is_(True))
            .order_by(Ad.sort.asc(), Ad.id.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is not None:
        return row
    return (
        await session.execute(
            select(Ad).where(Ad.active.is_(True)).order_by(Ad.sort.asc(), Ad.id.asc()).limit(1)
        )
    ).scalar_one_or_none()


async def list_ads(session: AsyncSession) -> list[Ad]:
    return list(
        (
            await session.execute(select(Ad).order_by(Ad.placement, Ad.sort, Ad.id))
        ).scalars().all()
    )


async def ad_block(session: AsyncSession, placement: str) -> tuple[str, list[InlineKeyboardButton]]:
    """Return a short HTML snippet and optional URL buttons for a screen."""
    from app.bot.menus import html_escape

    ad = await fetch_ad(session, placement)
    if ad is None:
        return "", []
    title = html_escape(ad.title)
    caption = html_escape(ad.caption or "")
    block = f"<b>✦ {title}</b>"
    if caption:
        block += f"\n{caption}"
    buttons: list[InlineKeyboardButton] = []
    if ad.url:
        buttons.append(InlineKeyboardButton(text=ad.title[:64], url=ad.url))
    return block, buttons


async def apply_ad(
    session: AsyncSession,
    placement: str,
    text: str,
    markup: InlineKeyboardMarkup | None,
) -> tuple[str, InlineKeyboardMarkup | None]:
    html, url_buttons = await ad_block(session, placement)
    if not html and not url_buttons:
        return text, markup
    new_text = f"{html}\n\n{text}" if html else text
    rows: list[list[InlineKeyboardButton]] = []
    if url_buttons:
        rows.append(url_buttons)
    if markup is not None:
        rows.extend(list(markup.inline_keyboard))
    return new_text, InlineKeyboardMarkup(inline_keyboard=rows)
