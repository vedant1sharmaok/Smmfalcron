"""FastAPI application: Mini App API, payment webhooks, static Mini App, health checks."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api.routes import router
from app.api.webhooks import router as webhook_router
from app.config import get_settings
from app.db import check_db

WEBAPP_DIR = Path(__file__).resolve().parent.parent / "webapp"
MAX_BODY_BYTES = 1_000_000

_MINI_APP_CSP = (
    "default-src 'self'; "
    "script-src 'self' https://telegram.org; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: https:; "
    "connect-src 'self'; "
    "base-uri 'none'; form-action 'none'; "
    "frame-ancestors https://web.telegram.org https://webk.telegram.org "
    "https://webz.telegram.org https://*.telegram.org"
)

_PAY_RETURN_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Payment</title>
<style>body{font-family:system-ui,sans-serif;background:#0f1115;color:#e8eaf0;display:grid;place-items:center;
min-height:100vh;margin:0;text-align:center;padding:24px}main{max-width:340px}h1{font-size:20px}
p{color:#9aa3b2;line-height:1.5}</style></head><body><main><h1>Thanks!</h1>
<p>If your payment went through, your wallet is credited automatically within a minute.
You can close this page and return to Telegram.</p></main></body></html>"""


def create_api() -> FastAPI:
    settings = get_settings()
    prod = settings.is_production
    app = FastAPI(
        title="FALARON Mini App API",
        version=__version__,
        docs_url=None if prod else "/docs",
        redoc_url=None,
        openapi_url=None if prod else "/openapi.json",
    )

    origins = settings.cors_origin_list
    wildcard = origins == ["*"]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=not wildcard,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization", "X-Telegram-Init-Data"],
        max_age=600,
    )

    @app.middleware("http")
    async def guard(request: Request, call_next):
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
            return JSONResponse({"detail": "Payload too large"}, status_code=413)
        response: Response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        path = request.url.path
        if path.startswith("/api") or path.startswith("/webhooks"):
            response.headers["Cache-Control"] = "no-store"
        elif path.startswith("/app"):
            response.headers.setdefault("Content-Security-Policy", _MINI_APP_CSP)
            if path in {"/app", "/app/", "/app/index.html"}:
                response.headers["Cache-Control"] = "no-cache"
        if prod:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response

    app.include_router(router)
    app.include_router(webhook_router)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": "falaron", "version": __version__}

    @app.get("/health/ready")
    async def ready() -> JSONResponse:
        ok = await check_db()
        return JSONResponse({"status": "ready" if ok else "degraded", "database": ok}, status_code=200 if ok else 503)

    @app.get("/pay/return", response_class=HTMLResponse)
    async def pay_return() -> HTMLResponse:
        return HTMLResponse(_PAY_RETURN_HTML)

    if WEBAPP_DIR.is_dir():
        app.mount("/app", StaticFiles(directory=str(WEBAPP_DIR), html=True), name="miniapp")

    return app


api_app = create_api()
