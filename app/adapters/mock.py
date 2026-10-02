"""In-process mock SMM panel. Orders progress on a timer so the UI is demo-able."""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from typing import Any

from app.adapters.base import (
    HealthResult,
    ProviderError,
    ProviderOrder,
    ProviderService,
    ProviderStatus,
)

log = logging.getLogger("falaron.adapter.mock")


def _normalize_status(raw: str) -> str:
    value = (raw or "").strip().lower().replace(" ", "_")
    mapping = {
        "pending": "pending",
        "in_progress": "in_progress",
        "inprogress": "in_progress",
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
    return mapping.get(value, "processing")


class MockAdapter:
    """Simulated panel. State lives in-process; a restart simply re-seeds open orders
    from the next status-sync (unknown ids are treated as in_progress).
    """

    def __init__(self, provider_id: str, *, fail_rate: float = 0.0) -> None:
        self.provider_id = provider_id
        self.fail_rate = fail_rate
        self._orders: dict[str, dict[str, Any]] = {}
        self._refills: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        self._balance = 10_000.00

    async def get_services(self) -> list[ProviderService]:
        return [
            ProviderService(
                external_id="mock-ig-likes",
                name="Instagram Likes",
                category="Instagram",
                rate=0.80,
                min_qty=50,
                max_qty=50_000,
                refill=True,
                cancel=True,
            ),
            ProviderService(
                external_id="mock-yt-views",
                name="YouTube Views",
                category="YouTube",
                rate=1.10,
                min_qty=100,
                max_qty=200_000,
                refill=True,
                cancel=True,
            ),
        ]

    async def get_balance(self) -> float:
        return self._balance

    async def create_order(
        self,
        service_external_id: str,
        link: str,
        quantity: int,
        extra: dict[str, Any] | None = None,
    ) -> ProviderOrder:
        if not link or not link.strip():
            raise ProviderError("Link is required")
        if quantity <= 0:
            raise ProviderError("Quantity must be positive")
        async with self._lock:
            external_id = f"M{secrets.token_hex(6).upper()}"
            now = time.time()
            self._orders[external_id] = {
                "id": external_id,
                "service": service_external_id,
                "link": link.strip(),
                "quantity": int(quantity),
                "extra": extra or {},
                "created_at": now,
                "start_count": 100 + (int(quantity) % 900),
                "status": "pending",
            }
            cost = round(quantity / 1000.0 * 0.9, 4)
            self._balance = max(0.0, self._balance - cost)
        log.info("mock create_order provider=%s id=%s qty=%s", self.provider_id, external_id, quantity)
        return ProviderOrder(external_id=external_id, raw={"order": external_id})

    def _progress(self, record: dict[str, Any]) -> dict[str, Any]:
        if record["status"] in {"completed", "canceled", "failed", "refunded", "partial"}:
            return record
        elapsed = time.time() - float(record["created_at"])
        qty = int(record["quantity"])
        if elapsed < 8:
            record["status"] = "pending"
            record["remains"] = qty
        elif elapsed < 25:
            record["status"] = "in_progress"
            record["remains"] = max(0, int(qty * (1 - (elapsed - 8) / 40)))
        elif elapsed < 45:
            record["status"] = "in_progress"
            record["remains"] = max(0, int(qty * 0.15))
        else:
            record["status"] = "completed"
            record["remains"] = 0
        return record

    async def get_order_status(self, external_order_id: str) -> ProviderStatus:
        async with self._lock:
            record = self._orders.get(external_order_id)
            if record is None:
                return ProviderStatus(
                    external_id=external_order_id,
                    status="in_progress",
                    start_count=None,
                    remains=None,
                    raw={"note": "unknown mock id; assuming in progress"},
                )
            record = self._progress(record)
            return ProviderStatus(
                external_id=external_order_id,
                status=_normalize_status(record["status"]),
                start_count=int(record.get("start_count") or 0),
                remains=int(record.get("remains") or 0),
                charge=None,
                raw={"status": record["status"]},
            )

    async def get_multiple_status(self, external_order_ids: list[str]) -> list[ProviderStatus]:
        out: list[ProviderStatus] = []
        for oid in external_order_ids:
            out.append(await self.get_order_status(oid))
        return out

    async def refill(self, external_order_id: str) -> str:
        async with self._lock:
            refill_id = f"R{secrets.token_hex(5).upper()}"
            self._refills[refill_id] = {
                "id": refill_id,
                "order": external_order_id,
                "created_at": time.time(),
                "status": "pending",
            }
            record = self._orders.get(external_order_id)
            if record is not None:
                record["status"] = "in_progress"
                record["created_at"] = time.time()
                record["remains"] = int(record.get("quantity") or 0)
        return refill_id

    async def refill_status(self, refill_id: str) -> ProviderStatus:
        async with self._lock:
            record = self._refills.get(refill_id)
            if record is None:
                raise ProviderError("Unknown refill id")
            elapsed = time.time() - float(record["created_at"])
            status = "completed" if elapsed > 20 else "pending"
            record["status"] = status
            return ProviderStatus(external_id=refill_id, status=status, raw=record)

    async def cancel(self, external_order_ids: list[str]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        async with self._lock:
            for oid in external_order_ids:
                record = self._orders.get(oid)
                if record is None:
                    result[oid] = "not found"
                    continue
                if record["status"] in {"completed", "canceled"}:
                    result[oid] = f"cannot cancel ({record['status']})"
                    continue
                record["status"] = "canceled"
                result[oid] = "canceled"
        return result

    async def health_check(self) -> HealthResult:
        return HealthResult(ok=True, detail="mock adapter ready", balance=self._balance)
