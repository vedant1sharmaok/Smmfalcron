"""Share-kit captions and logo photo helper used by referral + promoter panels."""

from __future__ import annotations

from urllib.parse import quote

from aiogram.types import CallbackQuery, FSInputFile, Message

from app.bot.menus import html_escape, share_photo_kb
from app.paths import LOGO_PATH


def deep_link(bot_username: str, code: str) -> str:
    username = (bot_username or "falaron_bot").lstrip("@")
    return f"https://t.me/{username}?start={quote(code, safe='')}"


def referral_captions(code: str, link: str) -> list[str]:
    return [
        (
            f"Grow faster with FALARON SMM — Instagram, YouTube, TikTok, Telegram & more. "
            f"Use my invite {code} for a ₹250 welcome bonus: {link}"
        ),
        (
            f"I use FALARON for real SMM delivery. ₹250 welcome bonus + 10% off with FALARON10. "
            f"Join with {code}: {link}"
        ),
        (
            f"FALARON — gold-standard social growth. Invite code {code}. Start here: {link}"
        ),
        (
            f"Need followers, views, members? FALARON SMM. Tap to start with my referral {code}: {link}"
        ),
    ]


def promoter_captions(code: str, link: str) -> list[str]:
    return [
        (
            f"Official FALARON promoter. Premium SMM for Instagram, YouTube, TikTok & Telegram. "
            f"Start with code {code}: {link}"
        ),
        (
            f"FALARON partner kit — ₹250 welcome bonus, coupon FALARON10, tracked delivery. "
            f"Open: {link}"
        ),
        (
            f"Scale your pages with FALARON. Promoter code {code}. Join the panel: {link}"
        ),
        (
            f"FALARON SMM · gold + purple growth. Promoter invite {code} → {link}"
        ),
    ]


def captions_for(kind: str, code: str, link: str) -> list[str]:
    return promoter_captions(code, link) if kind == "p" else referral_captions(code, link)


def picker_text(kind: str, code: str, link: str) -> str:
    variants = captions_for(kind, code, link)
    title = "Promoter share kit" if kind == "p" else "Share FALARON"
    lines = [
        f"<b>{title}</b>",
        "",
        f"Your code: <code>{html_escape(code)}</code>",
        f"Deep link: <code>{html_escape(link)}</code>",
        "",
        "Copy a caption below, or tap 1 / 2 / 3 / 4 to send the logo card.",
        "",
    ]
    for i, caption in enumerate(variants, start=1):
        lines.append(f"<b>{i}.</b> <code>{html_escape(caption)}</code>")
        lines.append("")
    return "\n".join(lines).rstrip()


async def resolve_bot_username(target: CallbackQuery | Message, configured: str | None) -> str:
    if configured:
        return configured.lstrip("@")
    bot = target.bot if hasattr(target, "bot") else None
    if bot is None:
        return "falaron_bot"
    me = await bot.get_me()
    return (me.username or "falaron_bot").lstrip("@")


async def send_share_card(
    target: CallbackQuery,
    *,
    caption: str,
    link: str,
) -> None:
    message = target.message if isinstance(target.message, Message) else None
    if message is None:
        return
    markup = share_photo_kb(link, caption)
    # Photo captions are plain-friendly; keep HTML off so t.me links stay clickable.
    if LOGO_PATH.is_file():
        photo = FSInputFile(str(LOGO_PATH), filename="falaron.jpg")
        await message.answer_photo(photo, caption=caption[:1024], reply_markup=markup)
        return
    await message.answer(caption, reply_markup=markup)
