"""
Shared pytest fixtures for integration tests.

All live-DB tests use per-test SAVEPOINT isolation:
  - BEGIN → SAVEPOINT test_sp → [test runs] → ROLLBACK TO SAVEPOINT test_sp
  - Nothing persists between tests; the outer transaction never commits.

Required env vars:
  DATABASE_URL   postgresql+asyncpg://user:pass@host/db
  RAZORPAY_WEBHOOK_SECRET   (optional — defaults to test value)
  BACKUP_ENCRYPTION_KEY     (optional — 64 hex chars)

Run:
  pytest tests/integration/ -v --tb=short
  pytest tests/integration/ -v -x          # stop on first failure
  pytest tests/integration/ -v -k wallet   # filter by keyword
"""

from __future__ import annotations

import asyncio
import os
import uuid
from decimal import Decimal
from typing import AsyncGenerator

import pytest

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+asyncpg://smm_app:dev_password_change_in_prod@localhost:5432/smm_platform",
)

# ── Skip marker ────────────────────────────────────────────────────────────────

def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "live_db: mark test as requiring a live PostgreSQL database",
    )
    config.addinivalue_line(
        "markers",
        "slow: mark test as slow (>5s)",
    )


def pytest_collection_modifyitems(config, items):
    """Auto-mark integration tests as live_db if they use the db fixture."""
    for item in items:
        if "db" in getattr(item, "fixturenames", []):
            item.add_marker(pytest.mark.live_db)


# ── Event loop (session-scoped) ────────────────────────────────────────────────

@pytest.fixture(scope="session")
def event_loop():
    """Single event loop for the entire test session."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


# ── Database engine (session-scoped) ──────────────────────────────────────────

@pytest.fixture(scope="session")
async def engine():
    """
    Async SQLAlchemy engine.
    Created once per session; disposed at end.
    Connection health is verified before yielding.
    """
    try:
        from sqlalchemy.ext.asyncio import create_async_engine
        from sqlalchemy import text

        eng = create_async_engine(
            DATABASE_URL,
            echo=False,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=10,
        )
        # Verify connectivity.
        async with eng.connect() as conn:
            await conn.execute(text("SELECT 1"))
        yield eng
        await eng.dispose()

    except ImportError:
        pytest.skip("sqlalchemy[asyncio] not installed")
    except Exception as exc:
        pytest.skip(f"Database not available: {exc}")


# ── Per-test isolated session ─────────────────────────────────────────────────

@pytest.fixture
async def db(engine) -> AsyncGenerator:
    """
    Per-test database session with SAVEPOINT isolation.
    The session rolls back to the savepoint after each test.
    No data ever commits to the database.
    """
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy import text

    async with engine.begin() as conn:
        await conn.execute(text("SAVEPOINT test_sp"))
        session = AsyncSession(bind=conn, expire_on_commit=False)
        try:
            yield session
        finally:
            await session.close()
            await conn.execute(text("ROLLBACK TO SAVEPOINT test_sp"))


# ── User factory ──────────────────────────────────────────────────────────────

@pytest.fixture
def user_factory(db):
    """
    Create a user row and return user_id (telegram_id).
    Usage: user_id = await user_factory()
    """
    async def _make(
        is_active: bool = True,
        is_banned: bool = False,
        is_premium: bool = False,
    ) -> int:
        from sqlalchemy import text
        tg_id = int(uuid.uuid4().int % (10**15))
        await db.execute(text(
            "INSERT INTO users"
            " (id, telegram_id, is_active, is_banned, is_premium, policy_version)"
            " VALUES (:id, :tg, :ia, :ib, :ip, 0)"
        ), {"id": tg_id, "tg": tg_id, "ia": is_active, "ib": is_banned, "ip": is_premium})
        await db.flush()
        return tg_id

    return _make


# ── Wallet factory ─────────────────────────────────────────────────────────────

@pytest.fixture
def wallet_factory(db, user_factory):
    """
    Create a user + wallet, return (wallet_id, user_id).
    Usage: wallet_id, user_id = await wallet_factory(balance=Decimal("500"))
    """
    async def _make(
        balance: Decimal = Decimal("100.00"),
        currency: str = "INR",
    ) -> tuple[int, int]:
        from sqlalchemy import text
        user_id = await user_factory()
        wallet_id = (await db.execute(text(
            "INSERT INTO wallets (user_id, balance, currency)"
            " VALUES (:uid, :bal, :cur) RETURNING id"
        ), {"uid": user_id, "bal": balance, "cur": currency})).scalar_one()
        await db.flush()
        return wallet_id, user_id

    return _make


# ── Provider factory ───────────────────────────────────────────────────────────

@pytest.fixture
def provider_factory(db):
    """Create a provider row, return provider_id."""
    _counter = [0]

    async def _make(
        name: str | None = None,
        endpoint: str = "https://api.test.com",
        currency: str = "USD",
        is_active: bool = True,
        health_status: str = "healthy",
    ) -> int:
        from sqlalchemy import text
        _counter[0] += 1
        n = name or f"TestProvider{_counter[0]}"
        slug = n.lower().replace(" ", "-")
        provider_id = (await db.execute(text(
            "INSERT INTO providers"
            " (name, slug, endpoint, currency, is_active, health_status, priority)"
            " VALUES (:n, :sl, :ep, :cur, :ia, :hs, 0) RETURNING id"
        ), {"n": n, "sl": slug, "ep": endpoint, "cur": currency,
            "ia": is_active, "hs": health_status})).scalar_one()
        await db.flush()
        return provider_id

    return _make


# ── Category factory ───────────────────────────────────────────────────────────

@pytest.fixture
def category_factory(db):
    """Create a category row, return category_id."""
    _counter = [0]

    async def _make(name: str | None = None) -> int:
        from sqlalchemy import text
        _counter[0] += 1
        n = name or f"Category{_counter[0]}"
        cat_id = (await db.execute(text(
            "INSERT INTO categories (name, slug, is_active)"
            " VALUES (:n, :sl, true) RETURNING id"
        ), {"n": n, "sl": n.lower().replace(" ", "-")})).scalar_one()
        await db.flush()
        return cat_id

    return _make


# ── Service factory ────────────────────────────────────────────────────────────

@pytest.fixture
def service_factory(db, provider_factory, category_factory):
    """
    Create a canonical service with a provider mapping.
    Returns dict: {service_id, public_id, provider_id, category_id}
    """
    async def _make(
        display_name: str = "IG Followers",
        provider_rate: Decimal = Decimal("1.50"),
        min_qty: int = 100,
        max_qty: int = 10000,
        is_active: bool = True,
    ) -> dict:
        from sqlalchemy import text
        provider_id = await provider_factory()
        category_id = await category_factory()

        # Provider service
        provider_svc_id = str(uuid.uuid4().int)[:6]
        await db.execute(text(
            "INSERT INTO provider_services"
            " (provider_id, provider_svc_id, raw_name, category, rate,"
            "  min_qty, max_qty, refill, cancel, drip_feed, is_active)"
            " VALUES (:pid, :psid, :n, 'Test', :r, :mn, :mx, true, false, false, :ia)"
        ), {"pid": provider_id, "psid": provider_svc_id, "n": display_name,
            "r": provider_rate, "mn": min_qty, "mx": max_qty, "ia": is_active})

        # Canonical service
        public_id = f"SVC-{str(uuid.uuid4().int)[:4].zfill(4)}"
        svc_id = (await db.execute(text(
            "INSERT INTO services"
            " (public_id, display_name, category_id, is_active,"
            "  ordering_enabled, refill_enabled, cancel_enabled)"
            " VALUES (:pid, :n, :cid, :ia, true, true, false) RETURNING id"
        ), {"pid": public_id, "n": display_name, "cid": category_id,
            "ia": is_active})).scalar_one()

        # Provider mapping
        await db.execute(text(
            "INSERT INTO service_provider_mappings"
            " (service_id, provider_id, provider_svc_id, is_primary, routing_weight)"
            " VALUES (:sid, :pid, :psid, true, 100)"
        ), {"sid": svc_id, "pid": provider_id, "psid": provider_svc_id})

        await db.flush()
        return {
            "service_id":   svc_id,
            "public_id":    public_id,
            "provider_id":  provider_id,
            "category_id":  category_id,
        }

    return _make


# ── Funded user factory (user + wallet + service + pricing rule) ──────────────

@pytest.fixture
def funded_user(db, wallet_factory, service_factory):
    """
    Full test context: user with ₹500 wallet, an active service,
    and a global 20% pricing rule. Returns everything needed for order tests.
    """
    async def _make(balance: Decimal = Decimal("500.00")) -> dict:
        from sqlalchemy import text
        wallet_id, user_id = await wallet_factory(balance=balance)
        svc = await service_factory()

        # Global 20% pricing rule, 5% min margin
        await db.execute(text(
            "INSERT INTO pricing_rules"
            " (scope, rule_type, value, min_margin_pct, currency, is_active)"
            " VALUES ('global', 'percentage', 20.0, 5.0, 'INR', true)"
        ))
        await db.flush()

        return {
            "user_id":     user_id,
            "wallet_id":   wallet_id,
            "service_id":  svc["service_id"],
            "public_id":   svc["public_id"],
            "provider_id": svc["provider_id"],
        }

    return _make


# ── Payment factory ────────────────────────────────────────────────────────────

@pytest.fixture
def payment_factory(db, user_factory):
    """Create a pending payment intent for a user."""
    async def _make(
        amount: Decimal = Decimal("500.00"),
        currency: str = "INR",
        provider: str = "razorpay",
        provider_ref: str | None = None,
        status: str = "pending",
    ) -> dict:
        from sqlalchemy import text
        user_id = await user_factory()
        await db.execute(text(
            "INSERT INTO wallets (user_id, balance, currency)"
            " VALUES (:uid, 0.00, :cur)"
        ), {"uid": user_id, "cur": currency})

        ref = provider_ref or f"SMM-{uuid.uuid4().hex[:8].upper()}"
        payment_id = (await db.execute(text(
            "INSERT INTO payments"
            " (user_id, amount, currency, provider, provider_ref,"
            "  status, webhook_verified)"
            " VALUES (:uid, :amt, :cur, :prov, :ref, :st, false)"
            " RETURNING id"
        ), {"uid": user_id, "amt": amount, "cur": currency,
            "prov": provider, "ref": ref, "st": status})).scalar_one()
        await db.flush()

        return {"payment_id": payment_id, "user_id": user_id, "ref": ref}

    return _make


# ── Coupon factory ─────────────────────────────────────────────────────────────

@pytest.fixture
def coupon_factory(db):
    """Create a coupon row, return its code."""
    from datetime import datetime, timezone

    async def _make(
        discount_type: str = "percentage",
        discount_value: Decimal = Decimal("10"),
        max_discount: Decimal | None = None,
        min_spend: Decimal | None = None,
        total_limit: int | None = None,
        per_user_limit: int = 1,
        expires_at: datetime | None = None,
        is_active: bool = True,
        is_transferable: bool = False,
        owner_user_id: int | None = None,
    ) -> str:
        from sqlalchemy import text
        code = "TEST" + uuid.uuid4().hex[:6].upper()
        await db.execute(text(
            "INSERT INTO coupons"
            " (code, discount_type, discount_value, max_discount,"
            "  min_spend, total_limit, per_user_limit, expires_at,"
            "  is_active, is_transferable, owner_user_id)"
            " VALUES"
            " (:code, :dt, :dv, :md, :ms, :tl, :pul, :ea, :ia, :it, :ou)"
        ), {"code": code, "dt": discount_type, "dv": discount_value,
            "md": max_discount, "ms": min_spend, "tl": total_limit,
            "pul": per_user_limit, "ea": expires_at,
            "ia": is_active, "it": is_transferable, "ou": owner_user_id})
        await db.flush()
        return code

    return _make


# ── Reseller profile factory ───────────────────────────────────────────────────

@pytest.fixture
def reseller_factory(db, user_factory):
    """Create a user + reseller profile, return dict."""
    async def _make(
        discount_pct: Decimal = Decimal("10"),
        min_margin_pct: Decimal = Decimal("5"),
        is_active: bool = True,
    ) -> dict:
        from sqlalchemy import text
        user_id = await user_factory()
        await db.execute(text(
            "INSERT INTO reseller_profiles"
            " (user_id, plan_name, discount_pct, min_margin_pct, is_active)"
            " VALUES (:uid, 'standard', :d, :m, :ia)"
        ), {"uid": user_id, "d": discount_pct, "m": min_margin_pct, "ia": is_active})
        await db.flush()
        return {"user_id": user_id, "discount_pct": discount_pct, "min_margin_pct": min_margin_pct}

    return _make


# ── Helpers ────────────────────────────────────────────────────────────────────

@pytest.fixture
def assert_wallet_balance(db):
    """Helper that fetches current wallet balance and asserts equality."""
    async def _check(user_id: int, expected: Decimal, msg: str = "") -> None:
        from sqlalchemy import text
        actual = (await db.execute(
            text("SELECT balance FROM wallets WHERE user_id = :uid"),
            {"uid": user_id},
        )).scalar_one()
        err = f"Balance mismatch for user {user_id}: expected={expected} actual={actual}"
        if msg:
            err += f" [{msg}]"
        assert actual == expected, err

    return _check


@pytest.fixture
def assert_tx_count(db):
    """Assert the number of wallet transactions for a user."""
    async def _check(user_id: int, expected_count: int) -> None:
        from sqlalchemy import text
        count = (await db.execute(text(
            "SELECT COUNT(*) FROM wallet_transactions wt"
            " JOIN wallets w ON w.id = wt.wallet_id"
            " WHERE w.user_id = :uid"
        ), {"uid": user_id})).scalar_one()
        assert count == expected_count, \
            f"Transaction count: expected={expected_count} actual={count}"

    return _check
