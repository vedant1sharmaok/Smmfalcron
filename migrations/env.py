"""
Alembic migration environment — async SQLAlchemy with asyncpg.
Compatible with Supabase session pooler (SSL).
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
# Make the project root importable.
#
# Render/Docker layout:
#   /app/
#       alembic.ini
#       app/
#       migrations/
#
# migrations/env.py is therefore one directory below the project root.
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
#
# Do NOT silently catch import errors here.
# If a model/dependency is broken, Alembic must show the real error.
# ---------------------------------------------------------------------------

from app.core.models import Base

target_metadata = Base.metadata


# ---------------------------------------------------------------------------
# Database URL
# ---------------------------------------------------------------------------

def _get_url() -> str:
    """
    Resolve the database URL.

    Priority:
      1. DATABASE_URL environment variable
      2. alembic.ini sqlalchemy.url

    Supports:
      postgresql://
      postgresql+asyncpg://
      postgres://

    In production, DATABASE_URL must be supplied by the environment.
    """

    # -----------------------------------------------------------------------
    # Prefer the actual environment variable.
    #
    # Render injects DATABASE_URL into the container environment.
    # -----------------------------------------------------------------------

    env_url = os.environ.get("DATABASE_URL", "").strip()

    if env_url:
        url = env_url
    else:
        # -------------------------------------------------------------------
        # Preserve the existing local-development fallback.
        #
        # This keeps old functionality intact while allowing local Alembic
        # usage through alembic.ini.
        # -------------------------------------------------------------------

        url = config.get_main_option(
            "sqlalchemy.url",
            "",
        ).strip()

    if not url:
        raise RuntimeError(
            "DATABASE_URL is missing. "
            "Set DATABASE_URL in the Render Environment Variables."
        )

    # -----------------------------------------------------------------------
    # Convert PostgreSQL URL to asyncpg URL.
    # -----------------------------------------------------------------------

    if url.startswith("postgres://"):
        url = url.replace(
            "postgres://",
            "postgresql+asyncpg://",
            1,
        )

    elif url.startswith("postgresql://"):
        url = url.replace(
            "postgresql://",
            "postgresql+asyncpg://",
            1,
        )

    # -----------------------------------------------------------------------
    # asyncpg does not use SQLAlchemy's sslmode=require query parameter.
    # Remove it because SSL is supplied through connect_args below.
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


# ---------------------------------------------------------------------------
# Online migration callback
# ---------------------------------------------------------------------------

def do_run_migrations(connection) -> None:
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
    url = _get_url()

    # Supabase requires SSL.
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
            await conn.run_sync(do_run_migrations)
    finally:
        await connectable.dispose()


# ---------------------------------------------------------------------------
# Online migration entry point
# ---------------------------------------------------------------------------

def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


# ---------------------------------------------------------------------------
# Alembic entry point
# ---------------------------------------------------------------------------

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
