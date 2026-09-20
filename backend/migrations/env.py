"""Alembic environment — wired to the application's settings and metadata."""
from __future__ import annotations

import logging
import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.core.config import settings  # noqa: E402
from app.db import Base  # noqa: E402
from app.models import models  # noqa: F401,E402  (register tables)

config = context.config
if config.config_file_name is not None:
    # fileConfig replaces the root logger's handlers, which destroys any handler
    # a caller has already installed (pytest's caplog is the common victim — a
    # migration test that runs fileConfig would silently empty caplog for every
    # test that follows in the same process). Save and restore the root handlers
    # so the migration run gets alembic's own formatting without side effects.
    _root = logging.getLogger()
    _saved_handlers = list(_root.handlers)
    _saved_level = _root.level
    try:
        fileConfig(config.config_file_name, disable_existing_loggers=False)
    finally:
        # Restore whatever the caller had — caplog, configure_logging, etc.
        for handler in list(_root.handlers):
            if handler not in _saved_handlers:
                _root.removeHandler(handler)
        for handler in _saved_handlers:
            if handler not in _root.handlers:
                _root.addHandler(handler)
        _root.setLevel(_saved_level)

target_metadata = Base.metadata

# Prefer an explicit CLI/env URL; otherwise use the app settings.
db_url = os.getenv("ALEMBIC_DATABASE_URL") or config.get_main_option("sqlalchemy.url") or settings.database_url
config.set_main_option("sqlalchemy.url", db_url.replace("%", "%%"))


def run_migrations_offline() -> None:
    context.configure(
        url=db_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_as_batch=db_url.startswith("sqlite"),
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
            # SQLite cannot ALTER most things; batch mode rewrites tables safely.
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
