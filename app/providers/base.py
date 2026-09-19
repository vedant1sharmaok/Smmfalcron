"""
BaseProviderAdapter — abstract base class for all SMM provider adapters.

Every provider adapter:
  1. Subclasses BaseProviderAdapter.
  2. Implements every abstract method.
  3. Keeps all provider-specific logic inside its own file.
  4. Returns canonical typed models — never raw dicts to the core.
  5. Never leaks credentials into logs, exceptions, or responses.

Shared HTTP client is built here once and reused by all adapters:
  - 30s connect / 60s read timeout (provider APIs can be slow)
  - 3 retries on connection errors and 5xx responses
  - TLS verified always
  - User-Agent identifies the platform, not the specific provider

Circuit breaker state is maintained externally in the registry;
adapters raise ProviderError/ProviderUnavailableError on failure
and the registry decides whether to trip the breaker.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from decimal import Decimal
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.core.exceptions import ProviderError, ProviderResponseError, ProviderUnavailableError
from app.core.logging import get_logger
from app.providers.models import (
    CancelResult,
    HealthResult,
    OrderRequest,
    ProviderHealthStatus,
    ProviderOrderResult,
    ProviderOrderStatus,
    ProviderRefillResult,
    ProviderRefillStatus,
    ProviderService,
)

logger = get_logger(__name__)

# ── Shared HTTP defaults ───────────────────────────────────────────────────────
_CONNECT_TIMEOUT = 10.0   # seconds
_READ_TIMEOUT    = 60.0   # provider APIs can be slow on large service lists
_MAX_RETRIES     = 3


def _build_http_client() -> httpx.AsyncClient:
    """
    Build a shared async HTTP client with sensible defaults.
    Caller is responsible for calling aclose() on shutdown.
    """
    return httpx.AsyncClient(
        timeout=httpx.Timeout(
            connect=_CONNECT_TIMEOUT,
            read=_READ_TIMEOUT,
            write=10.0,
            pool=5.0,
        ),
        limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
        headers={
            "User-Agent": "SMMPlatform/0.1 (+https://github.com/smm-platform)",
            "Accept": "application/json",
        },
        follow_redirects=False,
        verify=True,   # TLS verified; never disable in production
    )


class BaseProviderAdapter(ABC):
    """
    Abstract base for all provider adapters.

    Subclass and implement every abstract method.
    Call super().__init__(provider_id, name, endpoint, api_key) in __init__.

    The api_key passed here is the DECRYPTED key — decryption happens in the
    registry, not inside adapters.  Adapters must never log it.
    """

    def __init__(
        self,
        provider_id: int,
        name: str,
        endpoint: str,
        api_key: str,
    ) -> None:
        self.provider_id = provider_id
        self.name = name
        self.endpoint = endpoint.rstrip("/")
        self._api_key = api_key          # never log this
        self._client: httpx.AsyncClient | None = None
        self._logger = get_logger(f"provider.{name}")

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = _build_http_client()
        return self._client

    async def close(self) -> None:
        """Release the HTTP client. Called by the registry on shutdown."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    # ── Retry wrapper ──────────────────────────────────────────────────────────

    @staticmethod
    def _retryable(func):
        """
        Decorator applying exponential backoff on transient errors.
        ProviderError subclasses that indicate a permanent failure
        (bad API key, invalid service ID) should NOT be retried —
        callers raise ProviderResponseError for those.
        """
        return retry(
            retry=retry_if_exception_type((httpx.ConnectError, httpx.TimeoutException)),
            stop=stop_after_attempt(_MAX_RETRIES),
            wait=wait_exponential(multiplier=1, min=1, max=10),
            reraise=True,
        )(func)

    # ── Safe HTTP helpers ──────────────────────────────────────────────────────

    async def _post(self, data: dict[str, Any]) -> dict[str, Any]:
        """
        POST to self.endpoint with form data.
        Returns parsed JSON dict.
        Raises ProviderUnavailableError on network failures.
        Raises ProviderResponseError on non-200 or unparseable response.
        Never logs the api_key even if it appears in `data`.
        """
        safe_data = {k: v for k, v in data.items() if k != "key"}
        start = time.monotonic()
        try:
            client = await self._get_client()
            response = await client.post(self.endpoint, data=data)
            latency = round((time.monotonic() - start) * 1000, 1)

            self._logger.info(
                "provider_http_response",
                provider_id=self.provider_id,
                status_code=response.status_code,
                latency_ms=latency,
                action=safe_data.get("action"),
            )

            if response.status_code >= 500:
                raise ProviderUnavailableError(
                    detail=f"Provider {self.name} returned HTTP {response.status_code}",
                )
            if response.status_code >= 400:
                raise ProviderResponseError(
                    detail=f"Provider {self.name} returned HTTP {response.status_code}: "
                           f"{response.text[:200]}",
                )

            try:
                result = response.json()
            except Exception:
                raise ProviderResponseError(
                    detail=f"Provider {self.name} returned non-JSON: {response.text[:200]}",
                )

            # Many SMM panel APIs embed errors inside a 200 response.
            if isinstance(result, dict) and "error" in result:
                error_msg = str(result["error"])
                self._logger.warning(
                    "provider_api_error",
                    provider_id=self.provider_id,
                    error=error_msg,
                    action=safe_data.get("action"),
                )
                raise ProviderResponseError(
                    detail=f"Provider {self.name} API error: {error_msg}",
                )

            return result

        except (ProviderError, ProviderResponseError, ProviderUnavailableError):
            raise
        except httpx.TimeoutException as exc:
            self._logger.warning("provider_timeout", provider_id=self.provider_id)
            raise ProviderUnavailableError(
                detail=f"Provider {self.name} timed out",
            ) from exc
        except httpx.ConnectError as exc:
            self._logger.warning("provider_connect_error", provider_id=self.provider_id)
            raise ProviderUnavailableError(
                detail=f"Provider {self.name} connection failed",
            ) from exc
        except Exception as exc:
            self._logger.error(
                "provider_unexpected_error",
                provider_id=self.provider_id,
                error=type(exc).__name__,
            )
            raise ProviderError(
                detail=f"Provider {self.name} unexpected error: {type(exc).__name__}",
            ) from exc

    # ── Abstract interface ─────────────────────────────────────────────────────

    @abstractmethod
    async def get_services(self) -> list[ProviderService]:
        """
        Fetch the full service catalogue from the provider.
        Returns a list of ProviderService (normalised).
        Called by the sync engine periodically.
        """
        ...

    @abstractmethod
    async def get_balance(self) -> Decimal:
        """
        Return the current API balance in the provider's currency.
        Called by health checks and the admin balance display.
        """
        ...

    @abstractmethod
    async def create_order(self, req: OrderRequest) -> ProviderOrderResult:
        """
        Submit a new order to the provider.
        Returns ProviderOrderResult containing the provider's order ID.
        Raises ProviderResponseError on permanent failure (invalid service, etc.).
        Raises ProviderUnavailableError on transient failure.
        """
        ...

    @abstractmethod
    async def get_order_status(self, provider_order_id: str) -> ProviderOrderStatus:
        """Return the current status of a single order."""
        ...

    @abstractmethod
    async def get_multiple_order_status(
        self, provider_order_ids: list[str]
    ) -> list[ProviderOrderStatus]:
        """
        Batch status check.  More efficient than N single calls.
        If the provider doesn't support batch, loop internally.
        Returns one ProviderOrderStatus per ID; preserves order.
        """
        ...

    @abstractmethod
    async def refill(self, provider_order_id: str) -> ProviderRefillResult:
        """Request a refill for a completed or partial order."""
        ...

    @abstractmethod
    async def get_refill_status(self, refill_id: str) -> ProviderRefillStatus:
        """Return the status of a refill request."""
        ...

    @abstractmethod
    async def get_multiple_refill_status(
        self, refill_ids: list[str]
    ) -> list[ProviderRefillStatus]:
        """Batch refill status check."""
        ...

    @abstractmethod
    async def cancel(self, provider_order_id: str) -> CancelResult:
        """
        Request cancellation of a pending/in-progress order.
        Not all providers support this; raise ProviderResponseError if unsupported.
        """
        ...

    @abstractmethod
    async def health_check(self) -> HealthResult:
        """
        Lightweight check: verify connectivity and return balance.
        Used by the health worker every 5 minutes.
        Should complete in under 5 seconds.
        """
        ...

    # ── Concrete helpers available to all subclasses ───────────────────────────

    def _require_field(self, data: dict, field: str, context: str) -> Any:
        """
        Extract a required field from a provider response dict.
        Raises ProviderResponseError if missing.
        """
        if field not in data or data[field] is None:
            raise ProviderResponseError(
                detail=f"Provider {self.name} missing '{field}' in {context} response",
            )
        return data[field]

    def _safe_decimal(self, value: Any, fallback: Decimal | None = None) -> Decimal | None:
        """Safely convert a provider value to Decimal. Returns fallback on failure."""
        if value is None:
            return fallback
        try:
            return Decimal(str(value))
        except Exception:
            return fallback

    def _safe_int(self, value: Any, fallback: int | None = None) -> int | None:
        """Safely convert a provider value to int."""
        if value is None:
            return fallback
        try:
            return int(value)
        except Exception:
            return fallback
