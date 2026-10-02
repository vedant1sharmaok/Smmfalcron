"""Razorpay Payment Links gateway.

Flow: we create a payment link (reference_id = our PAY-XXXX id) -> the customer pays on
Razorpay's hosted page -> Razorpay calls our webhook with an HMAC signature -> we verify
signature, reference, amount and currency, then credit the ledger exactly once.
A periodic reconciliation pass asks Razorpay directly, so a lost webhook cannot lose money.

IMPORTANT: verify this integration end-to-end in Razorpay *test mode* before going live
(see DEPLOY.md). The field names below follow Razorpay's public Payment Links / webhook docs.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any, Mapping

from app.payments.base import (
    Checkout,
    GatewayError,
    GatewayEvent,
    GatewayStatus,
    SignatureError,
)
from app.payments.signing import verify_hmac_sha256_hex

_PLINK_RE = re.compile(r"^plink_[A-Za-z0-9]{6,40}$")
_REF_RE = re.compile(r"^PAY-[A-Z0-9]{4,16}$")


def _as_int(value: Any) -> int | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


class RazorpayGateway:
    name = "razorpay"

    def __init__(
        self,
        key_id: str,
        key_secret: str,
        webhook_secret: str,
        *,
        base_url: str = "https://api.razorpay.com/v1",
        timeout: float = 20.0,
    ) -> None:
        if not (key_id and key_secret and webhook_secret):
            raise ValueError("Razorpay key id, key secret and webhook secret are all required")
        self._key_id = key_id
        self._key_secret = key_secret
        self._webhook_secret = webhook_secret
        self._base = base_url.rstrip("/")
        self._timeout = timeout

    # ---- outbound -------------------------------------------------------
    async def _request(self, method: str, path: str, *, json_body: dict | None = None) -> dict[str, Any]:
        import httpx  # lazy: keeps this module importable without httpx installed

        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                auth=(self._key_id, self._key_secret),
                follow_redirects=False,
            ) as client:
                response = await client.request(method, f"{self._base}{path}", json=json_body)
        except httpx.HTTPError as exc:
            raise GatewayError("Razorpay network error") from exc
        try:
            data = response.json()
        except ValueError:
            data = {}
        if response.status_code >= 400:
            desc = ""
            if isinstance(data, dict):
                err = data.get("error")
                if isinstance(err, dict):
                    desc = str(err.get("description") or "")[:160]
            raise GatewayError(f"Razorpay HTTP {response.status_code} {desc}".strip())
        if not isinstance(data, dict):
            raise GatewayError("Razorpay returned an unexpected payload")
        return data

    async def create_checkout(
        self,
        *,
        reference_id: str,
        amount_paise: int,
        currency: str,
        description: str,
        notes: Mapping[str, str],
        callback_url: str | None,
    ) -> Checkout:
        if not _REF_RE.match(reference_id):
            raise GatewayError("Invalid payment reference")
        payload: dict[str, Any] = {
            "amount": int(amount_paise),
            "currency": currency,
            "accept_partial": False,
            "reference_id": reference_id,
            "description": description[:255],
            "notify": {"sms": False, "email": False},
            "reminder_enable": False,
            "notes": {str(k)[:40]: str(v)[:250] for k, v in notes.items()},
            "expire_by": int(time.time()) + 24 * 3600,  # Razorpay minimum is 15 minutes
        }
        if callback_url:
            payload["callback_url"] = callback_url
            payload["callback_method"] = "get"
        data = await self._request("POST", "/payment_links", json_body=payload)
        ref = str(data.get("id") or "")
        url = str(data.get("short_url") or "")
        if not _PLINK_RE.match(ref) or not url.startswith("https://"):
            raise GatewayError("Razorpay did not return a usable payment link")
        return Checkout(url=url, gateway_ref=ref)

    async def fetch_status(self, gateway_ref: str) -> GatewayStatus:
        if not _PLINK_RE.match(gateway_ref or ""):
            raise GatewayError("Invalid gateway reference")
        data = await self._request("GET", f"/payment_links/{gateway_ref}")
        status = str(data.get("status") or "").lower()
        if status == "paid":
            return GatewayStatus(
                state="paid",
                amount_paise=_as_int(data.get("amount_paid")),
                currency=str(data.get("currency") or "").upper() or None,
            )
        if status in {"expired", "cancelled", "canceled"}:
            return GatewayStatus(state="expired")
        return GatewayStatus(state="pending")

    # ---- inbound --------------------------------------------------------
    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> GatewayEvent:
        """Verify the signature over the raw body, then extract a normalised event."""
        lowered = {k.lower(): v for k, v in headers.items()}
        if not verify_hmac_sha256_hex(self._webhook_secret, body, lowered.get("x-razorpay-signature")):
            raise SignatureError("invalid webhook signature")
        try:
            data = json.loads(body)
        except ValueError as exc:
            raise GatewayError("webhook body is not JSON") from exc
        if not isinstance(data, dict):
            raise GatewayError("webhook body is not an object")

        event_type = str(data.get("event") or "")[:64]
        event_id = (lowered.get("x-razorpay-event-id") or "").strip()[:80]
        if not event_id:
            event_id = "sha256:" + hashlib.sha256(body).hexdigest()[:40]

        payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
        link = ((payload.get("payment_link") or {}).get("entity")) or {}
        pay = ((payload.get("payment") or {}).get("entity")) or {}
        link = link if isinstance(link, dict) else {}
        pay = pay if isinstance(pay, dict) else {}

        reference = str(link.get("reference_id") or "") or None
        gateway_ref = str(link.get("id") or "") or None
        currency = str(link.get("currency") or pay.get("currency") or "").upper() or None

        if event_type == "payment_link.paid":
            paid = _as_int(link.get("amount_paid"))
            pay_amount = _as_int(pay.get("amount"))
            status_ok = str(link.get("status") or "").lower() == "paid"
            if not reference or not _REF_RE.match(reference):
                return GatewayEvent(event_id, event_type, "ignored", None, gateway_ref, paid, currency,
                                    "no reference id (not one of our links)")
            if not status_ok or paid is None or paid <= 0 or (pay_amount is not None and pay_amount != paid):
                return GatewayEvent(event_id, event_type, "attention", reference, gateway_ref, paid, currency,
                                    "paid event with inconsistent status/amount")
            return GatewayEvent(event_id, event_type, "paid", reference, gateway_ref, paid, currency)

        if event_type in {"payment_link.expired", "payment_link.cancelled"}:
            return GatewayEvent(event_id, event_type, "expired", reference, gateway_ref, None, currency)

        if event_type.startswith(("refund.", "payment.dispute", "payment.refunded")):
            return GatewayEvent(event_id, event_type, "attention", reference, gateway_ref,
                                _as_int(pay.get("amount")), currency, "refund/dispute needs manual handling")

        return GatewayEvent(event_id, event_type, "ignored", reference, gateway_ref, None, currency)
