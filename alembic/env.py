"""
Alembic environment.

We use the synchronous sqlite:// URL for migrations (the app itself runs
sqlite+aiosqlite:// for async operation). The DB file path is shared.
"""

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import engine_from_config, pool

from alembic import context

# Make the app's modules importable when running `alembic` from somm-app/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import Base  # noqa: E402
import models  # noqa: E402,F401  (register all models on Base.metadata)


config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _resolve_db_url() -> str:
    """
    Prefer the DATABASE_URL env var; fall back to alembic.ini; always coerce
    to the synchronous SQLite driver — Alembic itself runs sync.
    """
    url = os.environ.get("DATABASE_URL") or config.get_main_option("sqlalchemy.url")
    if url and url.startswith("sqlite+aiosqlite"):
        url = url.replace("sqlite+aiosqlite", "sqlite", 1)
    return url


def run_migrations_offline() -> None:
    url = _resolve_db_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,  # SQLite needs batch mode for ALTER TABLE
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {}) or {}
    section["sqlalchemy.url"] = _resolve_db_url()
    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
