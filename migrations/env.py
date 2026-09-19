"""Alembic migration environment — async SQLAlchemy, Supabase SSL, DATABASE_URL from env."""
from __future__ import annotations
import asyncio, os, sys
from logging.config import fileConfig
from pathlib import Path

_here = Path(__file__).resolve().parent.parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here))

from alembic import context
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy import pool

config          = context.config
target_metadata = None

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

try:
    from app.core.models import Base
    target_metadata = Base.metadata
except Exception as exc:
    import warnings; warnings.warn(f"Could not import app models: {exc}")


def _get_url() -> str:
    url = os.environ.get("DATABASE_URL") or config.get_main_option("sqlalchemy.url", "")
    if not url:
        raise RuntimeError("DATABASE_URL not set")
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+asyncpg://", 1)
    elif url.startswith("postgresql://") and "+asyncpg" not in url:
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if "sslmode=require" in url:
        url = url.split("?")[0]
    return url


def _ssl_args(original_url: str) -> dict:
    if "supabase" in original_url or "sslmode=require" in original_url:
        return {"ssl": "require"}
    return {}


def run_migrations_offline() -> None:
    context.configure(url=_get_url(), target_metadata=target_metadata,
                      literal_binds=True, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    original = os.environ.get("DATABASE_URL", "")
    url      = _get_url()
    cfg      = config.get_section(config.config_ini_section, {})
    cfg["sqlalchemy.url"] = url
    connectable = async_engine_from_config(
        cfg, prefix="sqlalchemy.", poolclass=pool.NullPool,
        connect_args=_ssl_args(original),
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
