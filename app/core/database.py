"""
Async SQLAlchemy engine and session factory.

ROOT CAUSE FIX: Engine is created lazily, not at module import time.

On Render/production, environment variables are injected at runtime by the
platform. If the engine is built at import time, `settings.database_url`
returns the hardcoded localhost default (before Render's env vars are in
os.environ) → ConnectionRefusedError: [Errno 111] Connection refused.

Solution: defer engine creation to the first actual DB call.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import text

_engine = None
_session_factory = None


class Base(DeclarativeBase):
    pass


def _build_engine():
    """Build the SQLAlchemy async engine from current settings."""
    from app.core.config import settings

    url = settings.database_url
    connect_args: dict = {}

    # Supabase session pooler requires SSL.
    # asyncpg does not accept ?sslmode=require in the URL —
    # it must be passed as a connect_arg instead.
    if "sslmode=require" in url:
        url = url.split("?")[0]
        connect_args["ssl"] = "require"

    return create_async_engine(
        url,
        echo=False,
        pool_size=5,         # keep low for Supabase session pooler limits
        max_overflow=10,
        pool_pre_ping=True,
        pool_recycle=300,    # Supabase closes idle connections after ~5 min
        connect_args=connect_args,
    )


def get_engine():
    """Return the shared engine, creating it on first call."""
    global _engine
    if _engine is None:
        _engine = _build_engine()
    return _engine


def get_session_factory() -> async_sessionmaker:
    """Return the shared session factory, creating it on first call."""
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
    Return a new async session.

    Usage (matches the pattern used throughout the codebase):
        async with AsyncSessionLocal() as db:
            ...
    """
    return get_session_factory()()


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency — yields a per-request session with commit/rollback."""
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
