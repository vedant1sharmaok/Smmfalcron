"""Provider adapter protocol. Keys stay inside the adapter — never on Telegram."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


class ProviderError(Exception):
    def __init__(self, message: str, *, retryable: bool = False, payload: Any = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.payload = payload


@dataclass
class ProviderService:
    external_id: str
    name: str
    category: str
    rate: float
    min_qty: int
    max_qty: int
    service_type: str = "default"
    refill: bool = False
    cancel: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProviderOrder:
    external_id: str
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProviderStatus:
    external_id: str
    status: str
    start_count: int | None = None
    remains: int | None = None
    charge: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class HealthResult:
    ok: bool
    detail: str
    balance: float | None = None


@runtime_checkable
class ProviderAdapter(Protocol):
    provider_id: str

    async def get_services(self) -> list[ProviderService]: ...

    async def get_balance(self) -> float: ...

    async def create_order(
        self,
        service_external_id: str,
        link: str,
        quantity: int,
        extra: dict[str, Any] | None = None,
    ) -> ProviderOrder: ...

    async def get_order_status(self, external_order_id: str) -> ProviderStatus: ...

    async def get_multiple_status(self, external_order_ids: list[str]) -> list[ProviderStatus]: ...

    async def refill(self, external_order_id: str) -> str: ...

    async def refill_status(self, refill_id: str) -> ProviderStatus: ...

    async def cancel(self, external_order_ids: list[str]) -> dict[str, Any]: ...

    async def health_check(self) -> HealthResult: ...
