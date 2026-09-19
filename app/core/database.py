"""Lazy async engine — root cause fix for Render ConnectionRefusedError."""
from __future__ import annotations
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import text

_engine          = None
_session_factory = None

class Base(DeclarativeBase):
    pass

def _build_engine():
    from app.core.config import settings
    url = settings.database_url
    connect_args: dict = {}
    if "sslmode=require" in url:
        url = url.split("?")[0]
        connect_args["ssl"] = "require"
    return create_async_engine(
        url, echo=False, pool_size=5, max_overflow=10,
        pool_pre_ping=True, pool_recycle=300, connect_args=connect_args,
    )

def get_engine():
    global _engine
    if _engine is None:
        _engine = _build_engine()
    return _engine

def get_session_factory() -> async_sessionmaker:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            get_engine(), class_=AsyncSession, expire_on_commit=False
        )
    return _session_factory

def AsyncSessionLocal() -> AsyncSession:
    return get_session_factory()()

async def get_db():
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise

async def check_db_health() -> bool:
    try:
        async with AsyncSessionLocal() as db:
            await db.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
