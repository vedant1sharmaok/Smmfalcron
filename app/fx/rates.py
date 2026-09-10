"""
Live USD→INR FX rate fetcher.

Replaces the hardcoded Decimal("83") in the pricing engine.

Strategy:
  1. Check Redis cache (TTL 1 hour) — fastest path
  2. Try RBI reference rate API — authoritative for INR
  3. Try ExchangeRate-API (free tier) — broad fallback
  4. Use hardcoded fallback rate — never fail pricing

The rate is fetched and cached once per hour by the FX refresh worker job
(run_fx_rate_refresh, registered in WorkerSettings).
The pricing engine calls get_usd_to_inr_rate() which reads from cache
synchronously — no API call at order time.

Cache key: "fx:usd_inr"
Cache value: decimal string (e.g. "83.42")
Cache TTL: 3600s (1 hour)

Usage in pricing engine:
    from app.fx.rates import get_usd_to_inr_rate
    rate = await get_usd_to_inr_rate()
    # replaces: rate = settings.usd_to_inr_rate or Decimal("83")
"""

from __future__ import annotations

import asyncio
from decimal import Decimal, InvalidOperation
from typing import Optional

from app.core.logging import get_logger
from app.core.redis import get_redis

logger = get_logger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

_CACHE_KEY          = "fx:usd_inr"
_CACHE_TTL_S        = 3600          # 1 hour
_FALLBACK_RATE      = Decimal("83.00")
_MIN_VALID_RATE     = Decimal("60")
_MAX_VALID_RATE     = Decimal("130")
_REQUEST_TIMEOUT_S  = 10.0

# ── Public interface ───────────────────────────────────────────────────────────

async def get_usd_to_inr_rate() -> Decimal:
    """
    Get the current USD→INR exchange rate.

    Returns cached rate if available (< 1h old).
    Falls back to hardcoded rate on any error.
    Never raises — pricing must always complete.
    """
    try:
        redis = get_redis()
        cached = await redis.get(_CACHE_KEY)
        if cached:
            rate = Decimal(cached.decode())
            if _MIN_VALID_RATE <= rate <= _MAX_VALID_RATE:
                return rate
            logger.warning("fx_cached_rate_out_of_range", rate=str(rate))
    except Exception as exc:
        logger.warning("fx_cache_read_error", error=str(exc))

    # Cache miss or invalid — return fallback (refresher runs separately).
    logger.debug("fx_cache_miss_using_fallback", fallback=str(_FALLBACK_RATE))
    return _FALLBACK_RATE


async def refresh_usd_to_inr_rate() -> Decimal:
    """
    Fetch a fresh USD→INR rate from external sources and cache it.
    Called by the FX refresh worker job (every hour).

    Returns the rate that was cached (or fallback on total failure).
    """
    rate = await _fetch_rate_with_fallback()
    await _cache_rate(rate)
    return rate


# ── Fetchers ───────────────────────────────────────────────────────────────────

async def _fetch_rate_with_fallback() -> Decimal:
    """Try each source in order, return first valid rate."""
    sources = [
        ("rbi_reference",      _fetch_rbi),
        ("exchangerate_api",   _fetch_exchangerate_api),
        ("fixer_io",           _fetch_fixer_io),
    ]

    for name, fetcher in sources:
        try:
            rate = await asyncio.wait_for(fetcher(), timeout=_REQUEST_TIMEOUT_S)
            if rate and _MIN_VALID_RATE <= rate <= _MAX_VALID_RATE:
                logger.info("fx_rate_fetched", source=name, rate=str(rate))
                return rate
            else:
                logger.warning("fx_rate_invalid", source=name, rate=str(rate))
        except asyncio.TimeoutError:
            logger.warning("fx_source_timeout", source=name, timeout=_REQUEST_TIMEOUT_S)
        except Exception as exc:
            logger.warning("fx_source_error", source=name, error=str(exc))

    logger.error("fx_all_sources_failed_using_fallback", fallback=str(_FALLBACK_RATE))
    return _FALLBACK_RATE


async def _fetch_rbi() -> Optional[Decimal]:
    """
    Fetch USD/INR from RBI reference rate API.
    RBI publishes daily reference rates — accurate but updated once/day.

    Endpoint: https://www.rbi.org.in/scripts/bs_viewcontent.aspx?Id=2009
    (JSON endpoint subject to change — RBI doesn't have a formal public API)

    Fallback approach: scrape the FBIL (Financial Benchmarks India Ltd) JSON feed.
    FBIL publishes the official reference rate at:
    https://www.fbil.org.in/api/referencerates
    """
    try:
        import httpx
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_S) as client:
            resp = await client.get(
                "https://www.fbil.org.in/api/referencerates",
                headers={"Accept": "application/json"},
            )
            if resp.status_code != 200:
                return None

            data = resp.json()
            # FBIL response: {"rates": [{"currency": "USD", "rate": "83.42"}, ...]}
            rates = data.get("rates") or data.get("data") or []
            for item in rates:
                if item.get("currency", "").upper() == "USD":
                    return Decimal(str(item["rate"]))
    except Exception:
        pass

    return None


async def _fetch_exchangerate_api() -> Optional[Decimal]:
    """
    Fetch USD/INR from ExchangeRate-API (free tier, no key required).
    Endpoint: https://open.er-api.com/v6/latest/USD
    Free tier: 1500 requests/month — adequate for hourly refreshes.
    """
    try:
        import httpx
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_S) as client:
            resp = await client.get(
                "https://open.er-api.com/v6/latest/USD",
                headers={"Accept": "application/json"},
            )
            if resp.status_code != 200:
                return None

            data = resp.json()
            rates = data.get("rates", {})
            inr = rates.get("INR")
            if inr:
                return Decimal(str(inr)).quantize(Decimal("0.01"))
    except Exception:
        pass

    return None


async def _fetch_fixer_io() -> Optional[Decimal]:
    """
    Fetch USD/INR from Fixer.io (API key required in settings).
    settings.fixer_api_key — if not set, this source is skipped.
    """
    from app.core.config import settings
    api_key = getattr(settings, "fixer_api_key", "")
    if not api_key:
        return None

    try:
        import httpx
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_S) as client:
            resp = await client.get(
                "https://data.fixer.io/api/latest",
                params={"access_key": api_key, "symbols": "USD,INR", "base": "EUR"},
                headers={"Accept": "application/json"},
            )
            if resp.status_code != 200:
                return None

            data = resp.json()
            if not data.get("success"):
                return None

            rates = data.get("rates", {})
            usd_per_eur = rates.get("USD")
            inr_per_eur = rates.get("INR")
            if usd_per_eur and inr_per_eur and usd_per_eur > 0:
                # USD/INR = INR_per_EUR / USD_per_EUR
                rate = (Decimal(str(inr_per_eur)) / Decimal(str(usd_per_eur)))
                return rate.quantize(Decimal("0.01"))
    except Exception:
        pass

    return None


# ── Cache helpers ─────────────────────────────────────────────────────────────

async def _cache_rate(rate: Decimal) -> None:
    """Write the rate to Redis with _CACHE_TTL_S TTL."""
    try:
        redis = get_redis()
        await redis.set(_CACHE_KEY, str(rate), ex=_CACHE_TTL_S)
        logger.info("fx_rate_cached", rate=str(rate), ttl_s=_CACHE_TTL_S)
    except Exception as exc:
        logger.error("fx_cache_write_error", error=str(exc))


async def get_cached_rate_age() -> Optional[int]:
    """
    Return remaining TTL (in seconds) of the cached FX rate.
    Returns None if not cached.
    """
    try:
        redis = get_redis()
        ttl = await redis.ttl(_CACHE_KEY)
        if ttl < 0:
            return None
        return _CACHE_TTL_S - ttl   # age = total_ttl - remaining_ttl
    except Exception:
        return None
