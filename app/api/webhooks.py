"""Inbound payment webhooks. The signature is verified over the raw body before anything is parsed."""

from __future__ import annotations

import logging
from html import escape

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from app.api.limits import check_ip
from app.config import get_settings
from app.db import session_scope
from app.notify import notify_admins, notify_user
from app.payments.base import GatewayError, SignatureError
from app.payments.service import get_gateway, handle_webhook_event
from app.pricing import paise_to_rupees_str

log = logging.getLogger("falaron.webhooks")
router = APIRouter(prefix="/webhooks", tags=["webhooks"])

MAX_WEBHOOK_BYTES = 256 * 1024


@router.post("/razorpay")
async def razorpay_webhook(request: Request) -> JSONResponse:
    settings = get_settings()
    check_ip(request)
    gateway = get_gateway(settings)
    if gateway is None or gateway.name != "razorpay":
        raise HTTPException(status_code=404, detail="Not found")

    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_WEBHOOK_BYTES:
        raise HTTPException(status_code=413, detail="Payload too large")
    body = await request.body()
    if len(body) > MAX_WEBHOOK_BYTES:
        raise HTTPException(status_code=413, detail="Payload too large")

    try:
        event = gateway.parse_webhook(dict(request.headers), body)
    except SignatureError:
        log.warning("razorpay webhook rejected: bad signature (ip=%s)", request.client.host if request.client else "?")
        raise HTTPException(status_code=400, detail="Invalid signature") from None
    except GatewayError as exc:
        log.warning("razorpay webhook rejected: %s", exc)
        raise HTTPException(status_code=400, detail="Bad request") from None

    # Any exception here becomes a 500 and the whole transaction rolls back (including the
    # dedupe row), so Razorpay's retry is processed cleanly instead of being swallowed.
    async with session_scope() as session:
        outcome = await handle_webhook_event(session, "razorpay", event)

    log.info("razorpay event %s type=%s -> %s %s", event.event_id, event.event_type, outcome.outcome,
             outcome.payment_public_id or "")

    # Notifications happen after commit and are best-effort.
    if outcome.outcome == "credited" and outcome.user_id is not None:
        await notify_user(
            outcome.user_id,
            f"Payment received: <b>{paise_to_rupees_str(outcome.amount_paise or 0)}</b> added to your wallet "
            f"({escape(outcome.payment_public_id or '')}).",
        )
    elif outcome.outcome in {"amount_mismatch", "manual_review"}:
        await notify_admins(
            f"⚠️ Payment event needs manual review: <code>{escape(outcome.payment_public_id or '?')}</code> "
            f"({escape(event.event_type)}). Nothing was credited automatically."
        )
    return JSONResponse({"ok": True})
