"""FastAPI Prometheus instrumentation middleware."""
from __future__ import annotations
import re, time
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from app.monitoring.metrics import api_requests, api_duration, api_errors, generate_metrics

_PATH_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"/orders/ORD-\d{8}-[A-Z0-9]{8}"), "/orders/{order_ref}"),
    (re.compile(r"/services/SVC-\d+"),               "/services/{public_id}"),
    (re.compile(r"/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"), "/{uuid}"),
    (re.compile(r"/users/\d{7,10}"),                 "/users/{telegram_id}"),
    (re.compile(r"/(\d+)"),                          "/{id}"),
]
_SKIP_PATHS = frozenset({"/metrics", "/health", "/ready", "/favicon.ico"})


def _normalise_path(path: str) -> str:
    for pattern, replacement in _PATH_PATTERNS:
        path = pattern.sub(replacement, path)
    return path


class PrometheusMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path in _SKIP_PATHS:
            return await call_next(request)

        method    = request.method
        norm_path = _normalise_path(path)
        start     = time.monotonic()

        try:
            response = await call_next(request)
        except Exception as exc:
            elapsed = time.monotonic() - start
            api_requests.labels(method=method, endpoint=norm_path, status_code="500").inc()
            api_duration.labels(method=method, endpoint=norm_path).observe(elapsed)
            api_errors.labels(endpoint=norm_path).inc()
            raise

        elapsed = time.monotonic() - start
        api_requests.labels(method=method, endpoint=norm_path, status_code=str(response.status_code)).inc()
        api_duration.labels(method=method, endpoint=norm_path).observe(elapsed)
        if response.status_code >= 500:
            api_errors.labels(endpoint=norm_path).inc()
        return response


async def metrics_endpoint(request: Request) -> Response:
    from app.core.config import settings
    bearer = getattr(settings, "metrics_bearer_token", "")
    if bearer:
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or auth[7:] != bearer:
            return Response(content="Forbidden", status_code=403, media_type="text/plain")
    body, content_type = generate_metrics()
    return Response(content=body, status_code=200, media_type=content_type)
