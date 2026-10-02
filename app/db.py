"""Async SQLAlchemy engine, session factory, and schema bootstrap."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import Settings, get_settings

log = logging.getLogger("falaron.db")

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _attach_sqlite_pragmas(engine: AsyncEngine) -> None:
    @event.listens_for(engine.sync_engine, "connect")
    def _on_connect(dbapi_connection, _connection_record) -> None:  # type: ignore[no-untyped-def]
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()


def get_engine(settings: Settings | None = None) -> AsyncEngine:
    global _engine
    if _engine is not None:
        return _engine
    settings = settings or get_settings()
    kwargs: dict = {
        "echo": False,
        "pool_pre_ping": True,
        "future": True,
    }
    if settings.is_sqlite:
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    else:
        kwargs.update({"pool_size": 10, "max_overflow": 10, "pool_recycle": 1800})
    _engine = create_async_engine(settings.database_url, **kwargs)
    if settings.is_sqlite:
        _attach_sqlite_pragmas(_engine)
    return _engine


def get_session_factory(settings: Settings | None = None) -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is not None:
        return _session_factory
    _session_factory = async_sessionmaker(
        get_engine(settings),
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
    return _session_factory


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def _upgrade_schema_sync(sync_conn) -> list[str]:
    """Additive, idempotent schema upgrade for SQLite and PostgreSQL.

    `create_all` builds missing tables; this adds columns that were introduced after a table
    already existed. It never drops or rewrites anything. New columns must be nullable or carry a
    server default (enforced by code review; a NOT NULL column without a default is skipped and
    logged so the operator can migrate by hand).
    """
    from sqlalchemy import inspect
    from sqlalchemy.schema import CreateColumn

    from app.models import Base

    inspector = inspect(sync_conn)
    existing_tables = set(inspector.get_table_names())
    applied: list[str] = []
    dialect = sync_conn.dialect
    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue
        present = {col["name"] for col in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in present:
                continue
            if not column.nullable and column.server_default is None and not column.primary_key:
                log.error(
                    "cannot auto-add NOT NULL column %s.%s without a server default", table.name, column.name
                )
                continue
            ddl = str(CreateColumn(column).compile(dialect=dialect))
            sync_conn.execute(text(f"ALTER TABLE {table.name} ADD COLUMN {ddl}"))
            applied.append(f"{table.name}.{column.name}")
    return applied


async def init_db(settings: Settings | None = None) -> None:
    """Create tables if they do not exist, then apply additive column upgrades."""
    from app.models import Base

    settings = settings or get_settings()
    engine = get_engine(settings)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        applied = await conn.run_sync(_upgrade_schema_sync)
    for item in applied:
        log.warning("schema upgrade: added column %s", item)


async def check_db() -> bool:
    """Cheap connectivity probe used by /health/ready."""
    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001
        return False


async def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
