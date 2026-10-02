"""Gateway-neutral payment types. Import-light on purpose."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol


class GatewayError(Exception):
    """Gateway unreachable, rejected the request, or returned something unusable."""


class SignatureError(GatewayError):
    """Webhook signature missing or invalid. The body must be discarded."""


@dataclass(frozen=True)
class Checkout:
    url: str
    gateway_ref: str


@dataclass(frozen=True)
class GatewayEvent:
    event_id: str
    event_type: str
    # paid | expired | attention (refund/dispute: human must look) | ignored
    kind: str
    reference: str | None  # our Payment.public_id echoed back by the gateway
    gateway_ref: str | None
    amount_paise: int | None
    currency: str | None
    detail: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class GatewayStatus:
    state: str  # paid | expired | pending
    amount_paise: int | None = None
    currency: str | None = None


class PaymentGateway(Protocol):
    name: str

    async def create_checkout(
        self,
        *,
        reference_id: str,
        amount_paise: int,
        currency: str,
        description: str,
        notes: Mapping[str, str],
        callback_url: str | None,
    ) -> Checkout: ...

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> GatewayEvent: ...

    async def fetch_status(self, gateway_ref: str) -> GatewayStatus: ...
