"""PerfectPanel-style SMM panel adapter.

Standard form POST: key, action, plus action-specific fields.
Actions: services, balance, add, status, statuses, refill, refill_status, cancel.
The API key is held on this object only and is never logged.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.adapters.base import (
    HealthResult,
    ProviderError,
    ProviderOrder,
    ProviderService,
    ProviderStatus,
)

log = logging.getLogger("falaron.adapter.perfectpanel")

_STATUS_MAP = {
    "pending": "pending",
    "in progress": "in_progress",
    "in_progress": "in_progress",
    "processing": "processing",
    "completed": "completed",
    "complete": "completed",
    "partial": "partial",
    "canceled": "canceled",
    "cancelled": "canceled",
    "refunded": "refunded",
    "fail": "failed",
    "failed": "failed",
    "error": "failed",
}


def normalize_panel_status(raw: str | None) -> str:
    if not raw:
        return "processing"
    return _STATUS_MAP.get(str(raw).strip().lower(), "processing")


class PerfectPanelAdapter:
    def __init__(
        self,
        provider_id: str,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 30.0,
    ) -> None:
        if not base_url:
            raise ValueError("SMM panel URL is required")
        if not api_key:
            raise ValueError("SMM panel API key is required")
        self.provider_id = provider_id
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout

    def _client(self) -> httpx.AsyncClient:
        # Never follow redirects: the API key travels in the POST body and must not be replayed elsewhere.
        return httpx.AsyncClient(timeout=self._timeout, follow_redirects=False)

    async def _post(self, action: str, payload: dict[str, Any] | None = None) -> Any:
        data: dict[str, Any] = {"key": self._api_key, "action": action}
        if payload:
            data.update({k: v for k, v in payload.items() if v is not None})
        log.info("panel request provider=%s action=%s", self.provider_id, action)
        try:
            async with self._client() as client:
                response = await client.post(self._base_url, data=data)
                response.raise_for_status()
                body = response.json()
        except httpx.HTTPStatusError as exc:
            raise ProviderError(
                f"Panel HTTP {exc.response.status_code}",
                retryable=exc.response.status_code >= 500,
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderError("Panel network error", retryable=True) from exc
        except ValueError as exc:
            raise ProviderError("Panel returned non-JSON") from exc
        if isinstance(body, dict) and body.get("error"):
            raise ProviderError(str(body["error"]), payload=body)
        return body

    async def get_services(self) -> list[ProviderService]:
        body = await self._post("services")
        if not isinstance(body, list):
            raise ProviderError("Unexpected services payload")
        services: list[ProviderService] = []
        for item in body:
            if not isinstance(item, dict):
                continue
            try:
                services.append(
                    ProviderService(
                        external_id=str(item.get("service") or item.get("id")),
                        name=str(item.get("name") or "Unnamed"),
                        category=str(item.get("category") or "Other"),
                        rate=float(item.get("rate") or 0),
                        min_qty=int(item.get("min") or 1),
                        max_qty=int(item.get("max") or 1),
                        service_type=str(item.get("type") or "default"),
                        refill=bool(item.get("refill")),
                        cancel=bool(item.get("cancel")),
                        extra={k: item.get(k) for k in ("dripfeed", "description") if k in item},
                    )
                )
            except (TypeError, ValueError):
                continue
        return services

    async def get_balance(self) -> float:
        body = await self._post("balance")
        if not isinstance(body, dict):
            raise ProviderError("Unexpected balance payload")
        try:
            return float(body.get("balance") or 0)
        except (TypeError, ValueError) as exc:
            raise ProviderError("Invalid balance value") from exc

    async def create_order(
        self,
        service_external_id: str,
        link: str,
        quantity: int,
        extra: dict[str, Any] | None = None,
    ) -> ProviderOrder:
        payload: dict[str, Any] = {
            "service": service_external_id,
            "link": link,
            "quantity": quantity,
        }
        extra = extra or {}
        if extra.get("comments"):
            payload["comments"] = extra["comments"]
        if extra.get("usernames"):
            payload["usernames"] = extra["usernames"]
        if extra.get("runs"):
            payload["runs"] = extra["runs"]
        if extra.get("interval"):
            payload["interval"] = extra["interval"]
        body = await self._post("add", payload)
        if not isinstance(body, dict) or "order" not in body:
            raise ProviderError("Panel did not return an order id", payload=body)
        return ProviderOrder(external_id=str(body["order"]), raw=body)

    async def get_order_status(self, external_order_id: str) -> ProviderStatus:
        body = await self._post("status", {"order": external_order_id})
        if not isinstance(body, dict):
            raise ProviderError("Unexpected status payload")
        return self._to_status(external_order_id, body)

    async def get_multiple_status(self, external_order_ids: list[str]) -> list[ProviderStatus]:
        if not external_order_ids:
            return []
        joined = ",".join(external_order_ids)
        body = await self._post("status", {"orders": joined})
        out: list[ProviderStatus] = []
        if isinstance(body, dict):
            # Either a single status or a map of id -> status
            if "status" in body and len(external_order_ids) == 1:
                out.append(self._to_status(external_order_ids[0], body))
            else:
                for oid, item in body.items():
                    if isinstance(item, dict):
                        out.append(self._to_status(str(oid), item))
        return out

    async def refill(self, external_order_id: str) -> str:
        body = await self._post("refill", {"order": external_order_id})
        if isinstance(body, dict) and "refill" in body:
            return str(body["refill"])
        raise ProviderError("Panel did not accept refill", payload=body)

    async def refill_status(self, refill_id: str) -> ProviderStatus:
        body = await self._post("refill_status", {"refill": refill_id})
        if not isinstance(body, dict):
            raise ProviderError("Unexpected refill status payload")
        return self._to_status(refill_id, body)

    async def cancel(self, external_order_ids: list[str]) -> dict[str, Any]:
        """Cancel orders. Raises ProviderError unless the panel confirms *every* id.

        Panels answer with a list like [{"order": 9, "cancel": 1}] or
        [{"order": 9, "cancel": {"error": "Incorrect order ID"}}]. Treating a missing/failed
        entry as success would refund a customer for an order that is still running.
        """
        body = await self._post("cancel", {"orders": ",".join(external_order_ids)})
        confirmed: set[str] = set()
        if isinstance(body, list):
            for item in body:
                if isinstance(item, dict) and str(item.get("cancel")).strip().lower() in {"1", "true"}:
                    confirmed.add(str(item.get("order")))
        elif isinstance(body, dict) and len(external_order_ids) == 1:
            if str(body.get("cancel")).strip().lower() in {"1", "true"}:
                confirmed.add(external_order_ids[0])
        missing = [oid for oid in external_order_ids if str(oid) not in confirmed]
        if missing:
            raise ProviderError("The provider did not confirm the cancellation", payload=body)
        return {"canceled": sorted(confirmed)}

    async def health_check(self) -> HealthResult:
        try:
            balance = await self.get_balance()
            return HealthResult(ok=True, detail="panel reachable", balance=balance)
        except ProviderError as exc:
            return HealthResult(ok=False, detail=str(exc)[:200], balance=None)

    @staticmethod
    def _to_status(external_id: str, body: dict[str, Any]) -> ProviderStatus:
        def _maybe_int(value: Any) -> int | None:
            if value is None or value == "":
                return None
            try:
                return int(float(value))
            except (TypeError, ValueError):
                return None

        charge_raw = body.get("charge")
        try:
            charge = float(charge_raw) if charge_raw is not None else None
        except (TypeError, ValueError):
            charge = None
        return ProviderStatus(
            external_id=external_id,
            status=normalize_panel_status(str(body.get("status") or "")),
            start_count=_maybe_int(body.get("start_count")),
            remains=_maybe_int(body.get("remains")),
            charge=charge,
            raw=body,
        )
