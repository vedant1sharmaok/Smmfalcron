"""
Async SQLAlchemy engine and session factory.

Fix: _make_engine() was called at module level (line 24: engine = _make_engine()).
This ran at import time, before Render had injected DATABASE_URL into os.environ,
so settings.database_url returned the localhost default →
OSError: [Errno 111] Connect call failed ('127.0.0.1', 5432)

Fix: engine is created lazily on first use via get_engine().
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import text

_engine: object          = None
_session_factory: object = None


class Base(DeclarativeBase):
    pass


def _build_engine():
    """Build the async engine from current settings (called lazily)."""
    from app.core.config import settings

    url = settings.database_url

    # Supabase session pooler requires SSL.
    # asyncpg does not accept ?sslmode=require in the connection URL —
    # it must be passed as a connect_arg. Strip it and pass separately.
    connect_args: dict = {}
    if "sslmode=require" in url:
        url = url.split("?")[0]
        connect_args["ssl"] = "require"

    return create_async_engine(
        url,
        echo=False,
        pool_size=5,         # keep low — Supabase session pooler has connection limits
        max_overflow=10,
        pool_pre_ping=True,
        pool_recycle=300,    # Supabase closes idle connections after ~5 min
        connect_args=connect_args,
    )


def get_engine():
    """Return the shared engine, building it on first call."""
    global _engine
    if _engine is None:
        _engine = _build_engine()
    return _engine


def get_session_factory() -> async_sessionmaker:
    """Return the shared session factory, building it on first call."""
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
        )
    return _session_factory


def AsyncSessionLocal() -> AsyncSession:
    """
    Return a new async session context manager.

    Usage (matches pattern throughout codebase):
        async with AsyncSessionLocal() as db:
            ...
    """
    return get_session_factory()()


async def get_db():
    """FastAPI dependency — yields a per-request session."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def check_db_health() -> bool:
    """Ping the database. Returns True if reachable."""
    try:
        async with AsyncSessionLocal() as db:
            await db.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
