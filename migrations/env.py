"""
Alembic migration environment — async SQLAlchemy with asyncpg.
Compatible with Supabase session pooler (SSL).
"""
from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy import pool

config          = context.config
target_metadata = None

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Import all models so Alembic autogenerate sees them
try:
    from app.core.models import Base
    target_metadata = Base.metadata
except Exception as exc:
    import warnings
    warnings.warn(f"Could not import app models: {exc}")


def _get_url() -> str:
    """
    Resolve the database URL from the environment.

    Priority:
      1. DATABASE_URL environment variable (Render / production)
      2. alembic.ini sqlalchemy.url (local fallback)

    Supabase session pooler requires SSL. Strip ?sslmode=require
    from the URL — it will be passed as a connect_arg instead.
    """
    url = os.environ.get("DATABASE_URL") or config.get_main_option("sqlalchemy.url", "")

    # asyncpg doesn't accept ?sslmode=require — strip it.
    if "sslmode=require" in url:
        url = url.split("?")[0]

    # Alembic/SQLAlchemy needs postgresql+asyncpg:// for async engine
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+asyncpg://", 1)
    elif url.startswith("postgresql://") and "+asyncpg" not in url:
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)

    return url


def run_migrations_offline() -> None:
    url = _get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    url = _get_url()
    needs_ssl = "supabase" in url or os.environ.get("DATABASE_SSL", "") == "require"

    cfg = config.get_section(config.config_ini_section, {})
    cfg["sqlalchemy.url"] = url

    connect_args = {}
    if needs_ssl:
        connect_args["ssl"] = "require"

    connectable = async_engine_from_config(
        cfg,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        connect_args=connect_args,
    )
    async with connectable.connect() as conn:
        await conn.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
