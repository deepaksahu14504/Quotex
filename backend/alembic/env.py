"""Alembic environment.

Resolves the database URL from (in order): the URL Alembic was invoked with
(app/db.py's init_db() sets this from Settings.database_url at app startup),
then the DATABASE_URL env var (CLI usage: `alembic upgrade head`), so this
works identically whether migrations run automatically at app boot or are
run manually as part of a deploy pipeline.
"""
from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import metadata  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

if not config.get_main_option("sqlalchemy.url"):
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        try:
            from app.config import settings
            db_url = settings.database_url
        except Exception:
            db_url = None
    if db_url:
        # Same ConfigParser escaping as db.py: a DATABASE_URL containing a
        # percent-encoded character (a password with %40, or the options
        # parameter above) would otherwise raise
        # "invalid interpolation syntax" here on the `alembic` CLI path.
        config.set_main_option("sqlalchemy.url", db_url.replace("%", "%%"))

target_metadata = metadata


def run_migrations_offline() -> None:
    """Generate SQL against the target URL without a live DB connection
    (`alembic upgrade head --sql`)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
