"""
Telegram operational logging — Section 50.11 of blueprint.

Sends structured log messages to dedicated Telegram channels.
Each channel type has its own channel ID configured via env vars.
Messages are metadata-only — NEVER contain credentials, raw DB data,
provider API keys, or customer PII beyond what's needed for ops.

Channels:
  LOG_CH_PAYMENTS   — payment deposits + purchases
  LOG_CH_ORDERS     — order created, completed, failed, partial
  LOG_CH_REFILLS    — refill submitted, completed, failed
  LOG_CH_USERS      — new user registration, ban/unban
  LOG_CH_SECURITY   — failed auth, kill switch, fraud alerts
  LOG_CH_PROVIDERS  — sync completed, health change, degraded
  LOG_CH_ADMIN      — admin actions (credit/debit/ban/refund)
  LOG_CH_CRITICAL   — system errors, backup failures, DB issues
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from app.core.logging import get_logger

logger = get_logger(__name__)

# Channel IDs from environment (negative numbers for group/channel)
def _ch(key: str) -> Optional[int]:
    val = os.environ.get(key, "")
    try:
        return int(val) if val else None
    except ValueError:
        return None


class TelegramLogger:
    """
    Sends operational log messages to configured Telegram channels.
    All methods are fire-and-forget — never block the main flow.
    Failures are logged but silently swallowed.
    """

    def __init__(self, bot=None):
        self._bot = bot

    def _set_bot(self, bot) -> None:
        self._bot = bot

    async def _send(self, channel_id: Optional[int], text: str) -> None:
        if not channel_id or not self._bot:
            return
        try:
            await self._bot.send_message(
                chat_id=channel_id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception as exc:
            logger.debug("tg_log_send_failed", channel_id=channel_id, error=str(exc))

    # ── Payment events ────────────────────────────────────────────────────────

    async def deposit_received(
        self, user_id: int, amount: Decimal, provider: str, payment_ref: str
    ) -> None:
        ch = _ch("LOG_CH_PAYMENTS")
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        await self._send(ch,
            f"💰 <b>Deposit</b>  [{ts}]\n"
            f"User: <code>{user_id}</code>\n"
            f"Amount: <b>₹{amount:.2f}</b>\n"
            f"Provider: {provider.title()}\n"
            f"Ref: <code>{payment_ref}</code>"
        )

    async def payment_failed(
        self, user_id: int, amount: Decimal, reason: str
    ) -> None:
        ch = _ch("LOG_CH_PAYMENTS")
        await self._send(ch,
            f"❌ <b>Payment Failed</b>\n"
            f"User: <code>{user_id}</code>\n"
            f"Amount: ₹{amount:.2f}\n"
            f"Reason: {reason[:100]}"
        )

    # ── Order events ──────────────────────────────────────────────────────────

    async def order_placed(
        self, public_ref: str, user_id: int, service_name: str,
        quantity: int, price: Decimal, source: str
    ) -> None:
        ch = _ch("LOG_CH_ORDERS")
        await self._send(ch,
            f"📦 <b>Order Placed</b>\n"
            f"Ref: <code>{public_ref}</code>  [{source}]\n"
            f"User: <code>{user_id}</code>\n"
            f"Service: {service_name[:50]}\n"
            f"Qty: {quantity:,}  •  ₹{price:.2f}"
        )

    async def order_completed(
        self, public_ref: str, user_id: int
    ) -> None:
        ch = _ch("LOG_CH_ORDERS")
        await self._send(ch,
            f"✅ <b>Order Completed</b>\n"
            f"Ref: <code>{public_ref}</code>\n"
            f"User: <code>{user_id}</code>"
        )

    async def order_failed(
        self, public_ref: str, user_id: int, reason: str, refunded: bool
    ) -> None:
        ch = _ch("LOG_CH_ORDERS")
        ref_note = "  💸 Refunded" if refunded else ""
        await self._send(ch,
            f"🔴 <b>Order Failed</b>{ref_note}\n"
            f"Ref: <code>{public_ref}</code>\n"
            f"User: <code>{user_id}</code>\n"
            f"Reason: {reason[:100]}"
        )

    async def order_partial(
        self, public_ref: str, user_id: int, remains: int
    ) -> None:
        ch = _ch("LOG_CH_ORDERS")
        await self._send(ch,
            f"🟠 <b>Order Partial</b>\n"
            f"Ref: <code>{public_ref}</code>\n"
            f"User: <code>{user_id}</code>\n"
            f"Remains: {remains:,}"
        )

    # ── Refill events ─────────────────────────────────────────────────────────

    async def refill_submitted(
        self, public_ref: str, user_id: int
    ) -> None:
        ch = _ch("LOG_CH_REFILLS")
        await self._send(ch,
            f"🔁 <b>Refill Submitted</b>\n"
            f"Order: <code>{public_ref}</code>\n"
            f"User: <code>{user_id}</code>"
        )

    async def refill_completed(self, public_ref: str) -> None:
        ch = _ch("LOG_CH_REFILLS")
        await self._send(ch,
            f"✅ <b>Refill Complete</b>\n"
            f"Order: <code>{public_ref}</code>"
        )

    # ── User events ───────────────────────────────────────────────────────────

    async def user_registered(
        self, telegram_id: int, username: Optional[str]
    ) -> None:
        ch = _ch("LOG_CH_USERS")
        handle = f"@{username}" if username else f"<code>{telegram_id}</code>"
        await self._send(ch,
            f"👤 <b>New User</b>\n"
            f"TG: {handle}"
        )

    async def user_banned(
        self, user_id: int, actor_id: int, reason: str = ""
    ) -> None:
        ch = _ch("LOG_CH_USERS")
        await self._send(ch,
            f"🚫 <b>User Banned</b>\n"
            f"User: <code>{user_id}</code>\n"
            f"By: admin <code>{actor_id}</code>\n"
            + (f"Reason: {reason[:100]}" if reason else "")
        )

    async def user_unbanned(self, user_id: int, actor_id: int) -> None:
        ch = _ch("LOG_CH_USERS")
        await self._send(ch,
            f"✅ <b>User Unbanned</b>\n"
            f"User: <code>{user_id}</code>\n"
            f"By: admin <code>{actor_id}</code>"
        )

    # ── Security events ───────────────────────────────────────────────────────

    async def kill_switch_activated(
        self, switch_name: str, actor_id: int, reason: str
    ) -> None:
        ch = _ch("LOG_CH_SECURITY")
        await self._send(ch,
            f"🚨 <b>KILL SWITCH ACTIVATED</b>\n"
            f"Switch: <code>{switch_name}</code>\n"
            f"By: <code>{actor_id}</code>\n"
            f"Reason: {reason[:200]}"
        )

    async def kill_switch_deactivated(
        self, switch_name: str, actor_id: int
    ) -> None:
        ch = _ch("LOG_CH_SECURITY")
        await self._send(ch,
            f"✅ <b>Kill Switch Deactivated</b>\n"
            f"Switch: <code>{switch_name}</code>\n"
            f"By: <code>{actor_id}</code>"
        )

    async def suspicious_activity(
        self, user_id: int, event_type: str, details: str
    ) -> None:
        ch = _ch("LOG_CH_SECURITY")
        await self._send(ch,
            f"⚠️ <b>Security Event</b>: {event_type}\n"
            f"User: <code>{user_id}</code>\n"
            f"{details[:200]}"
        )

    async def auth_failed(self, identifier: str, reason: str) -> None:
        ch = _ch("LOG_CH_SECURITY")
        await self._send(ch,
            f"🔐 <b>Auth Failed</b>\n"
            f"ID: <code>{identifier[:20]}</code>\n"
            f"Reason: {reason[:100]}"
        )

    # ── Provider events ───────────────────────────────────────────────────────

    async def provider_sync_complete(
        self, provider_name: str, inserted: int, updated: int,
        deactivated: int, errors: int
    ) -> None:
        ch = _ch("LOG_CH_PROVIDERS")
        status = "✅" if errors == 0 else "⚠️"
        await self._send(ch,
            f"{status} <b>Sync: {provider_name}</b>\n"
            f"➕ {inserted}  ✏️ {updated}  🔴 {deactivated}  ❌ {errors}"
        )

    async def provider_degraded(
        self, provider_name: str, reason: str
    ) -> None:
        ch = _ch("LOG_CH_PROVIDERS")
        await self._send(ch,
            f"🔴 <b>Provider Degraded</b>: {provider_name}\n"
            f"Reason: {reason[:200]}"
        )

    async def provider_recovered(self, provider_name: str) -> None:
        ch = _ch("LOG_CH_PROVIDERS")
        await self._send(ch,
            f"✅ <b>Provider Recovered</b>: {provider_name}"
        )

    # ── Admin actions ─────────────────────────────────────────────────────────

    async def admin_wallet_adjustment(
        self, actor_id: int, target_user_id: int,
        amount: Decimal, tx_type: str, reason: str
    ) -> None:
        ch = _ch("LOG_CH_ADMIN")
        sign = "+" if "CREDIT" in tx_type else "-"
        await self._send(ch,
            f"💵 <b>Wallet Adjustment</b>\n"
            f"Admin: <code>{actor_id}</code>\n"
            f"User: <code>{target_user_id}</code>\n"
            f"Amount: {sign}₹{amount:.2f}  [{tx_type}]\n"
            f"Reason: {reason[:100]}"
        )

    async def admin_order_refund(
        self, actor_id: int, public_ref: str, amount: Decimal
    ) -> None:
        ch = _ch("LOG_CH_ADMIN")
        await self._send(ch,
            f"💸 <b>Admin Refund</b>\n"
            f"Admin: <code>{actor_id}</code>\n"
            f"Order: <code>{public_ref}</code>\n"
            f"Amount: ₹{amount:.2f}"
        )

    # ── Critical ──────────────────────────────────────────────────────────────

    async def critical_error(self, component: str, error: str) -> None:
        ch = _ch("LOG_CH_CRITICAL")
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        await self._send(ch,
            f"🚨 <b>CRITICAL ERROR</b>  [{ts}]\n"
            f"Component: {component}\n"
            f"Error: {error[:300]}"
        )

    async def backup_complete(
        self, size_bytes: int, sha256_prefix: str, s3_key: str
    ) -> None:
        ch = _ch("LOG_CH_CRITICAL")
        await self._send(ch,
            f"🗄️ <b>Backup Complete</b>\n"
            f"Size: {size_bytes // 1024 // 1024}MB\n"
            f"SHA256: <code>{sha256_prefix}...</code>\n"
            f"Key: <code>{s3_key}</code>"
        )

    async def backup_failed(self, error: str) -> None:
        ch = _ch("LOG_CH_CRITICAL")
        await self._send(ch,
            f"❌ <b>BACKUP FAILED</b>\n"
            f"Error: {error[:200]}"
        )


# Singleton — initialised with bot in lifespan
tg_logger = TelegramLogger()
