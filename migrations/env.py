"""
Alembic migration environment — async SQLAlchemy with asyncpg.

Compatible with:
- Render
- Supabase Session Pooler
- PostgreSQL
- postgresql://
- postgresql+asyncpg://
- postgres://

DATABASE_URL MUST be provided through the environment.
"""

from __future__ import annotations

import asyncio
import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config


# ---------------------------------------------------------------------------
# Project path
# ---------------------------------------------------------------------------

PROJECT_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..")
)

if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


# ---------------------------------------------------------------------------
# Alembic configuration
# ---------------------------------------------------------------------------

config = context.config


if config.config_file_name is not None:
    fileConfig(config.config_file_name)


# ---------------------------------------------------------------------------
# Import application models
# ---------------------------------------------------------------------------

from app.core.models import Base


target_metadata = Base.metadata


# ---------------------------------------------------------------------------
# Database URL
# ---------------------------------------------------------------------------

def _get_url() -> str:
    """
    Resolve the database URL.

    DATABASE_URL MUST come from the environment.

    This intentionally does NOT fall back to alembic.ini.
    That prevents Alembic from accidentally connecting to
    localhost:5432 on Render.

    Supported formats:
        postgresql://
        postgresql+asyncpg://
        postgres://
    """

    url = os.environ.get("DATABASE_URL", "").strip()

    if not url:
        raise RuntimeError(
            "DATABASE_URL is missing. "
            "Set DATABASE_URL in the Render Environment Variables."
        )

    # Render/Supabase may provide postgres://
    if url.startswith("postgres://"):
        url = url.replace(
            "postgres://",
            "postgresql+asyncpg://",
            1,
        )

    # Convert normal PostgreSQL URL to asyncpg URL
    elif url.startswith("postgresql://"):
        url = url.replace(
            "postgresql://",
            "postgresql+asyncpg://",
            1,
        )

    # -----------------------------------------------------------------------
    # asyncpg does not use sslmode=require as a normal libpq URL parameter.
    # Remove sslmode from the URL and configure SSL through connect_args.
    # -----------------------------------------------------------------------

    if "sslmode=" in url.lower():

        from urllib.parse import (
            urlsplit,
            urlunsplit,
            parse_qsl,
            urlencode,
        )

        parsed = urlsplit(url)

        query = [
            (key, value)
            for key, value in parse_qsl(
                parsed.query,
                keep_blank_values=True,
            )
            if key.lower() != "sslmode"
        ]

        url = urlunsplit(
            (
                parsed.scheme,
                parsed.netloc,
                parsed.path,
                urlencode(query),
                parsed.fragment,
            )
        )

    return url


# ---------------------------------------------------------------------------
# Offline migrations
# ---------------------------------------------------------------------------

def run_migrations_offline() -> None:
    """
    Run migrations in offline mode.

    This generates SQL without requiring a live database connection.
    """

    url = _get_url()

    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={
            "paramstyle": "named",
        },
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


# ---------------------------------------------------------------------------
# Online migration configuration
# ---------------------------------------------------------------------------

def do_run_migrations(connection) -> None:
    """
    Configure Alembic against an active database connection.
    """

    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


# ---------------------------------------------------------------------------
# Async migrations
# ---------------------------------------------------------------------------

async def run_async_migrations() -> None:
    """
    Create an async SQLAlchemy engine and execute Alembic migrations.
    """

    url = _get_url()

    # Supabase requires SSL.
    #
    # DATABASE_SSL=require can also be used explicitly on Render.
    needs_ssl = (
        "supabase" in url.lower()
        or os.environ.get(
            "DATABASE_SSL",
            "",
        ).lower() == "require"
    )

    cfg = config.get_section(
        config.config_ini_section,
        {},
    )

    if cfg is None:
        cfg = {}

    # Override alembic.ini with the real environment URL.
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

    try:
        async with connectable.connect() as conn:
            await conn.run_sync(
                do_run_migrations
            )

    finally:
        await connectable.dispose()


# ---------------------------------------------------------------------------
# Online migration entry point
# ---------------------------------------------------------------------------

def run_migrations_online() -> None:
    """
    Run Alembic migrations using an async PostgreSQL connection.
    """

    asyncio.run(
        run_async_migrations()
    )


# ---------------------------------------------------------------------------
# Alembic entry point
# ---------------------------------------------------------------------------

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
