"""
Alembic migration environment — async SQLAlchemy with asyncpg.

Issue 2 fix: "Could not import app models: No module named 'app'"
The container working directory is /app, so Python cannot find the 'app'
package unless /app is on sys.path. This env.py adds it explicitly.

Issue 6 fix: URL is read from DATABASE_URL env var FIRST, falling back
to alembic.ini's sqlalchemy.url only as a last resort. This guarantees
Render's injected DATABASE_URL is always used in production.

Issue 4 fix: sslmode=require is stripped from the URL and passed as a
connect_arg — asyncpg rejects it in the URL string.
"""
from __future__ import annotations

import asyncio
import os
import sys
from logging.config import fileConfig
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# ── CRITICAL: add the container root to sys.path ──────────────────────────────
# In the Docker image the app is at /app/app/... and the working dir is /app.
# Without this, `from app.core.models import Base` fails with:
#   ModuleNotFoundError: No module named 'app'
_here = Path(__file__).resolve().parent.parent   # directory containing 'app/'
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here))

from alembic import context
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy import pool

config          = context.config
target_metadata = None

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Import all models so Alembic autogenerate sees them
try:
    from app.core.models import Base          # noqa: E402
    target_metadata = Base.metadata
except Exception as exc:
    import warnings
    warnings.warn(f"Could not import app models: {exc}")


def _get_url() -> str:
    """
    Resolve the database URL.

    Priority order:
      1. DATABASE_URL environment variable  ← Render production
      2. alembic.ini sqlalchemy.url         ← local dev fallback

    Always converts to postgresql+asyncpg:// and strips ?sslmode=require.
    """
    url = (
        os.environ.get("DATABASE_URL")
        or config.get_main_option("sqlalchemy.url", "")
    )

    if not url:
        raise RuntimeError(
            "No database URL found. Set DATABASE_URL as an environment variable."
        )

    # Normalise driver prefix for asyncpg
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+asyncpg://", 1)
    elif url.startswith("postgresql://") and "+asyncpg" not in url:
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)

    # asyncpg rejects ?sslmode=require in the URL — strip it
    if "sslmode=require" in url:
        url = url.split("?")[0]

    return url


def _get_connect_args(url: str) -> dict:
    """Return SSL connect_args when connecting to Supabase."""
    # Use SSL if the URL is Supabase or if DATABASE_SSL=require is set
    if (
        "supabase" in url
        or os.environ.get("DATABASE_SSL", "").lower() == "require"
        or "sslmode=require" in os.environ.get("DATABASE_URL", "")
    ):
        return {"ssl": "require"}
    return {}


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
    url          = _get_url()
    connect_args = _get_connect_args(os.environ.get("DATABASE_URL", ""))

    cfg = config.get_section(config.config_ini_section, {})
    cfg["sqlalchemy.url"] = url

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
