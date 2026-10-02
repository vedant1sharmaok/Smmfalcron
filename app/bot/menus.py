"""Inline keyboards and message-edit helpers.

Always try to edit the existing message on callback. Swallow 'message is not modified'.
Button labels may use emoji; body copy stays mostly plain.
"""

from __future__ import annotations

import html as html_lib
from typing import Any, Iterable, Sequence
from urllib.parse import quote

from aiogram.exceptions import TelegramBadRequest
from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)

from app.models import ORDER_STATUS_LABELS, Category, Order, Service
from app.pricing import paise_to_rupees_str


class MenuCB(CallbackData, prefix="m"):
    a: str


class CatCB(CallbackData, prefix="c"):
    i: str


class SvcCB(CallbackData, prefix="s"):
    i: str
    a: str = "v"


class OrdCB(CallbackData, prefix="o"):
    i: str
    a: str = "v"


class WalCB(CallbackData, prefix="w"):
    a: str
    v: str = "-"


class AdmCB(CallbackData, prefix="ad"):
    a: str
    v: str = "-"


class RewCB(CallbackData, prefix="rw"):
    a: str
    v: str = "-"


class RsCB(CallbackData, prefix="rs"):
    a: str
    v: str = "-"


class PrCB(CallbackData, prefix="pr"):
    a: str
    v: str = "-"


class ShareCB(CallbackData, prefix="sh"):
    k: str  # r = referral, p = promoter
    v: str  # pick | 1 | 2 | 3 | 4


MAIN_ACTIONS = [
    ("🛍️ Services", "services"),
    ("💳 Deposit", "deposit"),
    ("📦 Orders", "orders"),
    ("👤 Profile", "profile"),
    ("📊 Stats", "stats"),
    ("💼 Balance", "balance"),
    ("♻️ Refills", "refills"),
    ("🎁 Rewards", "rewards"),
    ("💬 Support", "support"),
    ("⚙️ Settings", "settings"),
]


def _rows(buttons: Sequence[InlineKeyboardButton], per_row: int = 2) -> list[list[InlineKeyboardButton]]:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for button in buttons:
        row.append(button)
        if len(row) >= per_row:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return rows


def main_menu_kb(
    *,
    is_staff: bool = False,
    is_reseller: bool = False,
    is_promoter: bool = False,
    webapp_url: str | None = None,
) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(text=label, callback_data=MenuCB(a=action).pack())
        for label, action in MAIN_ACTIONS
    ]
    rows = _rows(buttons, 2)
    extra: list[InlineKeyboardButton] = []
    if is_reseller:
        extra.append(InlineKeyboardButton(text="🤝 Reseller", callback_data=RsCB(a="home").pack()))
    if is_promoter:
        extra.append(InlineKeyboardButton(text="📣 Promoter", callback_data=PrCB(a="home").pack()))
    if extra:
        rows.append(extra)
    if webapp_url:
        rows.append(
            [InlineKeyboardButton(text="🌐 Open Mini App", web_app=WebAppInfo(url=webapp_url))]
        )
    if is_staff:
        rows.append([InlineKeyboardButton(text="🛠️ Admin", callback_data=AdmCB(a="home").pack())])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_home_kb(extra: Iterable[list[InlineKeyboardButton]] | None = None) -> InlineKeyboardMarkup:
    rows = list(extra or [])
    rows.append(
        [
            InlineKeyboardButton(text="← Menu", callback_data=MenuCB(a="home").pack()),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def terms_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="I agree — continue", callback_data=MenuCB(a="accept_terms").pack()),
            ],
            [
                InlineKeyboardButton(text="Terms", callback_data=MenuCB(a="terms").pack()),
                InlineKeyboardButton(text="Privacy", callback_data=MenuCB(a="privacy").pack()),
            ],
        ]
    )


def categories_kb(categories: Sequence[Category]) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(
            text=f"{cat.emoji} {cat.name}".strip(),
            callback_data=CatCB(i=cat.id).pack(),
        )
        for cat in categories
    ]
    rows = _rows(buttons, 2)
    rows.append([InlineKeyboardButton(text="← Menu", callback_data=MenuCB(a="home").pack())])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def services_kb(category_id: str, services: Sequence[Service]) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=svc.name,
                callback_data=SvcCB(i=svc.id, a="v").pack(),
            )
        ]
        for svc in services
    ]
    rows.append([InlineKeyboardButton(text="← Categories", callback_data=MenuCB(a="services").pack())])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def service_detail_kb(service: Service) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🛒 Order", callback_data=SvcCB(i=service.id, a="o").pack())],
            [InlineKeyboardButton(text="← Back", callback_data=CatCB(i=service.category_id).pack())],
        ]
    )


def order_confirm_kb(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Confirm & pay", callback_data=SvcCB(i=token, a="ok").pack()),
            ],
            [
                InlineKeyboardButton(text="Cancel", callback_data=MenuCB(a="services").pack()),
            ],
        ]
    )


def orders_kb(orders: Sequence[Order], page: int, has_next: bool) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"{order.public_id} · {ORDER_STATUS_LABELS.get(order.status, order.status)}",
                callback_data=OrdCB(i=order.public_id, a="v").pack(),
            )
        ]
        for order in orders
    ]
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="← Prev", callback_data=OrdCB(i=str(page - 1), a="p").pack()))
    if has_next:
        nav.append(InlineKeyboardButton(text="Next →", callback_data=OrdCB(i=str(page + 1), a="p").pack()))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="← Menu", callback_data=MenuCB(a="home").pack())])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def order_detail_kb(order: Order) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(text="🔄 Refresh", callback_data=OrdCB(i=order.public_id, a="r").pack()),
        ]
    ]
    actions: list[InlineKeyboardButton] = []
    if order.status in {"completed", "partial"}:
        actions.append(
            InlineKeyboardButton(text="♻️ Refill", callback_data=OrdCB(i=order.public_id, a="f").pack())
        )
    if order.status in {"pending", "awaiting_provider", "processing", "in_progress"}:
        actions.append(
            InlineKeyboardButton(text="Cancel", callback_data=OrdCB(i=order.public_id, a="x").pack())
        )
    if actions:
        rows.append(actions)
    rows.append([InlineKeyboardButton(text="← Orders", callback_data=MenuCB(a="orders").pack())])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def cancel_confirm_kb(public_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Yes, cancel", callback_data=OrdCB(i=public_id, a="xx").pack()),
                InlineKeyboardButton(text="Keep it", callback_data=OrdCB(i=public_id, a="v").pack()),
            ]
        ]
    )


DEPOSIT_AMOUNTS_PAISE = [10_000, 25_000, 50_000, 100_000, 250_000, 500_000]


def deposit_kb() -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(
            text=paise_to_rupees_str(amount),
            callback_data=WalCB(a="amt", v=str(amount)).pack(),
        )
        for amount in DEPOSIT_AMOUNTS_PAISE
    ]
    rows = _rows(buttons, 3)
    rows.append([InlineKeyboardButton(text="← Menu", callback_data=MenuCB(a="home").pack())])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def mock_invoice_kb(public_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Pay now (demo)", callback_data=WalCB(a="pay", v=public_id).pack())],
            [InlineKeyboardButton(text="← Deposit", callback_data=MenuCB(a="deposit").pack())],
        ]
    )


def pay_link_kb(url: str, public_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 Pay securely", url=url)],
            [InlineKeyboardButton(text="🔄 I have paid — check", callback_data=WalCB(a="chk", v=public_id).pack())],
            [InlineKeyboardButton(text="← Deposit", callback_data=MenuCB(a="deposit").pack())],
        ]
    )


def manual_invoice_kb(public_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Check status", callback_data=WalCB(a="chk", v=public_id).pack())],
            [InlineKeyboardButton(text="← Deposit", callback_data=MenuCB(a="deposit").pack())],
        ]
    )


def profile_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📜 Ledger", callback_data=MenuCB(a="ledger").pack()),
                InlineKeyboardButton(text="📊 Stats", callback_data=MenuCB(a="stats").pack()),
            ],
            [
                InlineKeyboardButton(text="🎁 Rewards", callback_data=MenuCB(a="rewards").pack()),
                InlineKeyboardButton(text="💬 Support", callback_data=MenuCB(a="support").pack()),
            ],
            [
                InlineKeyboardButton(text="⚙️ Settings", callback_data=MenuCB(a="settings").pack()),
            ],
            [InlineKeyboardButton(text="← Menu", callback_data=MenuCB(a="home").pack())],
        ]
    )


def settings_kb(notify: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=("🔔 Notifications on" if notify else "🔕 Notifications off"),
                    callback_data=MenuCB(a="toggle_notify").pack(),
                )
            ],
            [
                InlineKeyboardButton(text="Terms", callback_data=MenuCB(a="terms").pack()),
                InlineKeyboardButton(text="Privacy", callback_data=MenuCB(a="privacy").pack()),
            ],
            [InlineKeyboardButton(text="← Menu", callback_data=MenuCB(a="home").pack())],
        ]
    )


def rewards_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📤 Share", callback_data=ShareCB(k="r", v="pick").pack()),
            ],
            [
                InlineKeyboardButton(text="🤝 Become reseller", callback_data=RsCB(a="apply").pack()),
                InlineKeyboardButton(text="📣 Become promoter", callback_data=PrCB(a="apply").pack()),
            ],
            [InlineKeyboardButton(text="← Menu", callback_data=MenuCB(a="home").pack())],
        ]
    )


def share_picker_kb(kind: str) -> InlineKeyboardMarkup:
    back = (
        MenuCB(a="rewards").pack()
        if kind == "r"
        else PrCB(a="home").pack()
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="1", callback_data=ShareCB(k=kind, v="1").pack()),
                InlineKeyboardButton(text="2", callback_data=ShareCB(k=kind, v="2").pack()),
                InlineKeyboardButton(text="3", callback_data=ShareCB(k=kind, v="3").pack()),
                InlineKeyboardButton(text="4", callback_data=ShareCB(k=kind, v="4").pack()),
            ],
            [InlineKeyboardButton(text="← Back", callback_data=back)],
        ]
    )


def share_photo_kb(link: str, caption: str) -> InlineKeyboardMarkup:
    share = (
        "https://t.me/share/url?url="
        + quote(link, safe="")
        + "&text="
        + quote(caption, safe="")
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📤 Share", url=share)],
            [InlineKeyboardButton(text="Open FALARON", url=link)],
            [InlineKeyboardButton(text="← Menu", callback_data=MenuCB(a="home").pack())],
        ]
    )


def reseller_home_kb(*, can_order: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(text="🛒 Place order", callback_data=MenuCB(a="services").pack()),
            InlineKeyboardButton(text="👥 Downline", callback_data=RsCB(a="down").pack()),
        ],
        [
            InlineKeyboardButton(text="💸 Request payout", callback_data=RsCB(a="pay").pack()),
        ],
        [InlineKeyboardButton(text="← Menu", callback_data=MenuCB(a="home").pack())],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def promoter_home_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📤 Share kit", callback_data=ShareCB(k="p", v="pick").pack()),
            ],
            [
                InlineKeyboardButton(text="💸 Request payout", callback_data=PrCB(a="pay").pack()),
            ],
            [InlineKeyboardButton(text="← Menu", callback_data=MenuCB(a="home").pack())],
        ]
    )


def apply_kb(kind: str) -> InlineKeyboardMarkup:
    action = RsCB(a="apply").pack() if kind == "reseller" else PrCB(a="apply").pack()
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Apply", callback_data=action)],
            [InlineKeyboardButton(text="← Menu", callback_data=MenuCB(a="home").pack())],
        ]
    )


def admin_home_kb(flags: dict[str, bool]) -> InlineKeyboardMarkup:
    def mark(key: str, label: str) -> str:
        return f"{'●' if flags.get(key) else '○'} {label}"

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📈 Dashboard", callback_data=AdmCB(a="dash").pack())],
            [
                InlineKeyboardButton(
                    text=mark("kill_new_orders", "Orders"),
                    callback_data=AdmCB(a="k", v="kill_new_orders").pack(),
                ),
                InlineKeyboardButton(
                    text=mark("kill_payments", "Payments"),
                    callback_data=AdmCB(a="k", v="kill_payments").pack(),
                ),
            ],
            [
                InlineKeyboardButton(
                    text=mark("kill_refills", "Refills"),
                    callback_data=AdmCB(a="k", v="kill_refills").pack(),
                ),
                InlineKeyboardButton(
                    text=mark("read_only", "Read-only"),
                    callback_data=AdmCB(a="k", v="read_only").pack(),
                ),
            ],
            [
                InlineKeyboardButton(
                    text=mark("global_maintenance", "Maintenance"),
                    callback_data=AdmCB(a="k", v="global_maintenance").pack(),
                ),
            ],
            [
                InlineKeyboardButton(text="👤 Lookup user", callback_data=AdmCB(a="lookup").pack()),
                InlineKeyboardButton(text="💰 Credit / debit", callback_data=AdmCB(a="credit").pack()),
            ],
            [
                InlineKeyboardButton(text="🔌 Providers", callback_data=AdmCB(a="prov").pack()),
                InlineKeyboardButton(text="🔄 Sync health", callback_data=AdmCB(a="health").pack()),
            ],
            [
                InlineKeyboardButton(text="🪧 Ads", callback_data=AdmCB(a="ads").pack()),
                InlineKeyboardButton(text="📝 Applications", callback_data=AdmCB(a="apps").pack()),
            ],
            [InlineKeyboardButton(text="← Menu", callback_data=MenuCB(a="home").pack())],
        ]
    )


def admin_back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="← Admin", callback_data=AdmCB(a="home").pack())]]
    )


async def safe_edit(
    target: CallbackQuery | Message,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    *,
    parse_mode: str | None = "HTML",
) -> Any:
    """Edit the callback message, or send a new one if editing is impossible."""
    message: Message | None
    if isinstance(target, CallbackQuery):
        try:
            await target.answer()
        except TelegramBadRequest:
            pass
        message = target.message if isinstance(target.message, Message) else None
    else:
        message = target
    if message is None:
        return None
    try:
        return await message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except TelegramBadRequest as exc:
        desc = str(exc).lower()
        if "message is not modified" in desc:
            return message
        if "there is no text in the message" in desc:
            try:
                return await message.edit_caption(caption=text, reply_markup=reply_markup, parse_mode=parse_mode)
            except TelegramBadRequest:
                return await message.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)
        if "message to edit not found" in desc or "message can't be edited" in desc:
            return await message.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)
        if "can't parse" in desc or "can't find end tag" in desc:
            return await message.edit_text(text, reply_markup=reply_markup, parse_mode=None)
        raise


def html_escape(value: str) -> str:
    return html_lib.escape(value or "", quote=False)
