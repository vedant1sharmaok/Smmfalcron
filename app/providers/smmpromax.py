"""
SMMProMax provider adapter.

Implements the standard SMM panel HTTP API used by the majority
of SMM reseller panels (the same contract as the PHP reference
provided in the project specification).

API contract:
  POST {endpoint}
  Content-Type: application/x-www-form-urlencoded

  key={api_key}&action={action}&[action-specific params]

Actions implemented:
  services      → get_services()
  balance       → get_balance()
  add           → create_order()
  status        → get_order_status()
  multi-status  → get_multiple_order_status()  (batches of ≤100)
  refill        → refill()
  refill_status → get_refill_status()
  multi_refill  → get_multiple_refill_status() (batches of ≤100)
  cancel        → cancel()

Provider errors are embedded inside 200 responses as {"error": "..."}.
The base class _post() handles this and raises ProviderResponseError.

NEVER expose self._api_key in logs, exceptions, or return values.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.core.exceptions import ProviderResponseError, ProviderUnavailableError
from app.core.logging import get_logger
from app.providers.base import BaseProviderAdapter
from app.providers.models import (
    CancelResult,
    HealthResult,
    OrderRequest,
    OrderStatus,
    ProviderHealthStatus,
    ProviderOrderResult,
    ProviderOrderStatus,
    ProviderRefillResult,
    ProviderRefillStatus,
    ProviderService,
    RefillStatus,
)

logger = get_logger(__name__)

# Provider API batch limits.
_MAX_MULTI_STATUS = 100
_MAX_MULTI_REFILL = 100


class SMMProMaxAdapter(BaseProviderAdapter):
    """
    Adapter for the standard SMM panel API.
    Compatible with smmpromax.com, cheappanel.com, and any panel
    running the same API contract.
    """

    def _base_payload(self, action: str) -> dict[str, Any]:
        """Build the base payload with key and action."""
        return {"key": self._api_key, "action": action}

    # ── Services ───────────────────────────────────────────────────────────────

    async def get_services(self) -> list[ProviderService]:
        """
        Fetch full service catalogue.
        Response: list of service objects.
        """
        payload = self._base_payload("services")
        raw = await self._post(payload)

        if not isinstance(raw, list):
            raise ProviderResponseError(
                detail=f"Provider {self.name} services: expected list, got {type(raw).__name__}",
            )

        services: list[ProviderService] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            svc_id = item.get("service") or item.get("id")
            name   = item.get("name") or item.get("description", "")
            if not svc_id or not name:
                logger.warning(
                    "provider_service_missing_fields",
                    provider_id=self.provider_id,
                    item_keys=list(item.keys()),
                )
                continue

            services.append(ProviderService(
                provider_svc_id=str(svc_id),
                name=str(name),
                category=str(item.get("category", "Uncategorised")),
                type=str(item.get("type", "")) or None,
                rate=self._safe_decimal(item.get("rate")),
                min_qty=self._safe_int(item.get("min")),
                max_qty=self._safe_int(item.get("max")),
                refill=bool(item.get("refill", False)),
                cancel=bool(item.get("cancel", False)),
                drip_feed=bool(item.get("dripfeed", False)),
                description=item.get("description"),
                raw=item,
            ))

        logger.info(
            "provider_services_fetched",
            provider_id=self.provider_id,
            count=len(services),
        )
        return services

    # ── Balance ────────────────────────────────────────────────────────────────

    async def get_balance(self) -> Decimal:
        """
        Fetch current API wallet balance.
        Response: {"balance": "123.45", "currency": "USD"}
        """
        payload = self._base_payload("balance")
        raw = await self._post(payload)

        balance_raw = raw.get("balance")
        if balance_raw is None:
            raise ProviderResponseError(
                detail=f"Provider {self.name} balance response missing 'balance' field",
            )

        balance = self._safe_decimal(balance_raw)
        if balance is None:
            raise ProviderResponseError(
                detail=f"Provider {self.name} balance is not a valid number: {balance_raw!r}",
            )

        return balance

    # ── Order creation ─────────────────────────────────────────────────────────

    async def create_order(self, req: OrderRequest) -> ProviderOrderResult:
        """
        Submit a new order.
        Response: {"order": 12345}
        """
        payload = self._base_payload("add")
        payload["service"] = req.provider_svc_id
        payload["quantity"] = str(req.quantity)

        if req.link:
            payload["link"] = req.link

        # drip-feed / custom fields (e.g. username, comments, hashtags)
        for k, v in req.custom_fields.items():
            if k not in payload:
                payload[k] = str(v)

        raw = await self._post(payload)

        order_id = raw.get("order")
        if not order_id:
            raise ProviderResponseError(
                detail=f"Provider {self.name} add order: missing 'order' field in response",
            )

        logger.info(
            "provider_order_created",
            provider_id=self.provider_id,
            provider_order_id=str(order_id),
        )
        return ProviderOrderResult(
            provider_order_id=str(order_id),
            status=OrderStatus.PENDING,
            raw=raw,
        )

    # ── Order status ───────────────────────────────────────────────────────────

    async def get_order_status(self, provider_order_id: str) -> ProviderOrderStatus:
        """
        Single order status.
        Response: {"charge": "0.24", "start_count": "3610", "status": "Completed",
                   "remains": "0", "currency": "USD"}
        """
        payload = self._base_payload("status")
        payload["order"] = provider_order_id
        raw = await self._post(payload)

        return ProviderOrderStatus(
            provider_order_id=provider_order_id,
            status=OrderStatus.from_provider_string(
                str(raw.get("status", "processing"))
            ),
            start_count=self._safe_int(raw.get("start_count")),
            remains=self._safe_int(raw.get("remains")),
            currency=raw.get("currency"),
            charge=self._safe_decimal(raw.get("charge")),
            raw=raw,
        )

    async def get_multiple_order_status(
        self, provider_order_ids: list[str]
    ) -> list[ProviderOrderStatus]:
        """
        Batch order status — comma-separated IDs, max 100 per call.
        Response: {"12345": {"charge": ..., "start_count": ..., ...}, ...}
        Falls back to sequential single-status calls if batch is not supported.
        """
        results: list[ProviderOrderStatus] = []

        for i in range(0, len(provider_order_ids), _MAX_MULTI_STATUS):
            chunk = provider_order_ids[i:i + _MAX_MULTI_STATUS]
            payload = self._base_payload("status")
            payload["orders"] = ",".join(chunk)

            try:
                raw = await self._post(payload)
            except ProviderResponseError:
                # Provider doesn't support multi-status — fall back to single calls.
                for oid in chunk:
                    try:
                        results.append(await self.get_order_status(oid))
                    except ProviderResponseError as exc:
                        logger.warning(
                            "provider_status_fallback_failed",
                            provider_id=self.provider_id,
                            provider_order_id=oid,
                            error=str(exc),
                        )
                continue

            if not isinstance(raw, dict):
                # Some panels return a list — iterate positionally.
                for oid, item in zip(chunk, raw if isinstance(raw, list) else []):
                    results.append(ProviderOrderStatus(
                        provider_order_id=oid,
                        status=OrderStatus.from_provider_string(
                            str(item.get("status", "processing"))
                        ),
                        start_count=self._safe_int(item.get("start_count")),
                        remains=self._safe_int(item.get("remains")),
                        raw=item,
                    ))
                continue

            for oid in chunk:
                item = raw.get(oid) or raw.get(str(oid)) or {}
                results.append(ProviderOrderStatus(
                    provider_order_id=oid,
                    status=OrderStatus.from_provider_string(
                        str(item.get("status", "processing"))
                    ),
                    start_count=self._safe_int(item.get("start_count")),
                    remains=self._safe_int(item.get("remains")),
                    currency=item.get("currency"),
                    charge=self._safe_decimal(item.get("charge")),
                    raw=item,
                ))

        return results

    # ── Refill ─────────────────────────────────────────────────────────────────

    async def refill(self, provider_order_id: str) -> ProviderRefillResult:
        """
        Request a refill.
        Response: {"refill": 67890}
        """
        payload = self._base_payload("refill")
        payload["order"] = provider_order_id
        raw = await self._post(payload)

        refill_id = raw.get("refill")
        if not refill_id:
            raise ProviderResponseError(
                detail=f"Provider {self.name} refill: missing 'refill' field",
            )

        return ProviderRefillResult(
            refill_id=str(refill_id),
            raw=raw,
        )

    async def get_refill_status(self, refill_id: str) -> ProviderRefillStatus:
        """
        Single refill status.
        Response: {"status": "Completed"} or {"status": "Rejected"}
        """
        payload = self._base_payload("refill_status")
        payload["refill"] = refill_id
        raw = await self._post(payload)

        status_raw = str(raw.get("status", "pending")).lower().strip()
        if "complet" in status_raw:
            status = RefillStatus.COMPLETED
        elif "reject" in status_raw:
            status = RefillStatus.REJECTED
        elif "fail" in status_raw or "error" in status_raw:
            status = RefillStatus.FAILED
        else:
            status = RefillStatus.PENDING

        return ProviderRefillStatus(
            refill_id=refill_id,
            status=status,
            raw=raw,
        )

    async def get_multiple_refill_status(
        self, refill_ids: list[str]
    ) -> list[ProviderRefillStatus]:
        """
        Batch refill status — comma-separated, max 100 per call.
        Response: {"67890": {"status": "Completed"}, ...}
        """
        results: list[ProviderRefillStatus] = []

        for i in range(0, len(refill_ids), _MAX_MULTI_REFILL):
            chunk = refill_ids[i:i + _MAX_MULTI_REFILL]
            payload = self._base_payload("refill_status")
            payload["refills"] = ",".join(chunk)

            try:
                raw = await self._post(payload)
            except ProviderResponseError:
                # Fall back to single calls.
                for rid in chunk:
                    try:
                        results.append(await self.get_refill_status(rid))
                    except Exception as exc:
                        logger.warning(
                            "provider_refill_status_fallback_failed",
                            provider_id=self.provider_id,
                            refill_id=rid,
                            error=str(exc),
                        )
                continue

            for rid in chunk:
                item = raw.get(rid) or raw.get(str(rid)) or {}
                status_raw = str(item.get("status", "pending")).lower()
                if "complet" in status_raw:
                    status = RefillStatus.COMPLETED
                elif "reject" in status_raw:
                    status = RefillStatus.REJECTED
                elif "fail" in status_raw:
                    status = RefillStatus.FAILED
                else:
                    status = RefillStatus.PENDING

                results.append(ProviderRefillStatus(
                    refill_id=rid,
                    status=status,
                    raw=item,
                ))

        return results

    # ── Cancel ─────────────────────────────────────────────────────────────────

    async def cancel(self, provider_order_id: str) -> CancelResult:
        """
        Request cancellation.
        Response: [{"order": 12345, "cancel": {"1": "success"}}] or error.
        """
        payload = self._base_payload("cancel")
        payload["orders"] = provider_order_id

        try:
            raw = await self._post(payload)
        except ProviderResponseError as exc:
            return CancelResult(
                success=False,
                provider_order_id=provider_order_id,
                message=str(exc),
                raw={},
            )

        # Parse the varied cancel response formats across providers.
        success = False
        if isinstance(raw, list) and raw:
            item = raw[0]
            cancel_data = item.get("cancel", {})
            if isinstance(cancel_data, dict):
                success = any("success" in str(v).lower() for v in cancel_data.values())
        elif isinstance(raw, dict):
            success = "success" in str(raw).lower()

        return CancelResult(
            success=success,
            provider_order_id=provider_order_id,
            raw=raw if isinstance(raw, dict) else {"raw": str(raw)},
        )

    # ── Health check ───────────────────────────────────────────────────────────

    async def health_check(self) -> HealthResult:
        """
        Lightweight connectivity check using the balance endpoint.
        Fast: single HTTP round-trip.
        """
        import time
        start = time.monotonic()
        try:
            balance = await self.get_balance()
            latency = round((time.monotonic() - start) * 1000, 1)
            return HealthResult(
                status=ProviderHealthStatus.HEALTHY,
                balance=balance,
                latency_ms=latency,
            )
        except ProviderUnavailableError as exc:
            return HealthResult(
                status=ProviderHealthStatus.DOWN,
                error=str(exc),
                latency_ms=round((time.monotonic() - start) * 1000, 1),
            )
        except Exception as exc:
            return HealthResult(
                status=ProviderHealthStatus.DEGRADED,
                error=f"{type(exc).__name__}: {exc}",
                latency_ms=round((time.monotonic() - start) * 1000, 1),
            )
