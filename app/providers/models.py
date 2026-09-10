"""
Provider data models — pure Python dataclasses.

These are the internal canonical representations that cross the boundary
between provider adapters and the application core.  Every adapter
normalises its raw API response into these types.

Rules:
- No ORM imports here.  These are DTOs.
- Decimal for all monetary values.  Never float.
- Optional fields default to None — not every provider exposes every field.
- provider_svc_id is always the provider's own ID string.
  It is NEVER exposed to customers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any


class ProviderHealthStatus(StrEnum):
    HEALTHY  = "healthy"
    DEGRADED = "degraded"   # elevated error rate, still usable
    DOWN     = "down"       # circuit breaker tripped, not usable


class OrderStatus(StrEnum):
    PENDING    = "pending"
    PROCESSING = "processing"
    IN_PROGRESS = "in_progress"
    COMPLETED  = "completed"
    PARTIAL    = "partial"
    CANCELLED  = "cancelled"
    FAILED     = "failed"
    REFUNDED   = "refunded"

    @classmethod
    def from_provider_string(cls, raw: str) -> "OrderStatus":
        """
        Normalise provider-specific status strings to canonical values.
        Providers use inconsistent capitalisation and naming.
        Unknown strings default to PROCESSING to avoid false-terminal states.
        """
        _MAP = {
            "pending":      cls.PENDING,
            "in progress":  cls.IN_PROGRESS,
            "inprogress":   cls.IN_PROGRESS,
            "processing":   cls.PROCESSING,
            "completed":    cls.COMPLETED,
            "complete":     cls.COMPLETED,
            "partial":      cls.PARTIAL,
            "canceled":     cls.CANCELLED,
            "cancelled":    cls.CANCELLED,
            "failed":       cls.FAILED,
            "error":        cls.FAILED,
            "refunded":     cls.REFUNDED,
        }
        return _MAP.get(raw.lower().strip(), cls.PROCESSING)


class RefillStatus(StrEnum):
    PENDING    = "pending"
    COMPLETED  = "completed"
    REJECTED   = "rejected"
    FAILED     = "failed"


@dataclass
class ProviderService:
    """One service as returned by the provider's /services endpoint."""
    provider_svc_id: str
    name: str
    category: str
    type: str | None = None
    rate: Decimal | None = None          # cost per 1000 units, provider currency
    min_qty: int | None = None
    max_qty: int | None = None
    refill: bool = False
    cancel: bool = False
    drip_feed: bool = False
    description: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)  # full raw response, never exposed


@dataclass
class OrderRequest:
    """Parameters for submitting a new order to a provider."""
    provider_svc_id: str                 # provider's own service ID
    quantity: int
    link: str | None = None
    custom_fields: dict[str, Any] = field(default_factory=dict)
    idempotency_key: str | None = None   # passed as comment/reference where supported


@dataclass
class ProviderOrderResult:
    """Result from a successful order submission."""
    provider_order_id: str
    status: OrderStatus = OrderStatus.PENDING
    start_count: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProviderOrderStatus:
    """Status of an existing order."""
    provider_order_id: str
    status: OrderStatus
    start_count: int | None = None
    remains: int | None = None
    currency: str | None = None
    charge: Decimal | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProviderRefillResult:
    """Result from requesting a refill."""
    refill_id: str
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProviderRefillStatus:
    """Status of an existing refill."""
    refill_id: str
    status: RefillStatus
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class CancelResult:
    """Result from cancelling an order."""
    success: bool
    provider_order_id: str
    message: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class HealthResult:
    """Result from a provider health check."""
    status: ProviderHealthStatus
    balance: Decimal | None = None
    latency_ms: float | None = None
    error: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)
