"""
Database engine / session / migration bootstrap.

* Postgres is the production target (``DATABASE_URL=postgresql+psycopg2://…``).
* SQLite still works for local dev and tests (WAL + foreign keys enabled).
* Schema changes are applied with Alembic (``AUTO_MIGRATE=true``); tests and
  throwaway environments can fall back to ``create_all``.
"""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import settings

log = logging.getLogger(__name__)

IS_SQLITE = settings.database_url.startswith("sqlite")

# Ensure data directories exist before the engine touches them.
os.makedirs(settings.upload_dir, exist_ok=True)
os.makedirs(settings.generated_dir, exist_ok=True)


def _engine_kwargs() -> dict:
    kwargs: dict = {"pool_pre_ping": True, "future": True}
    if IS_SQLITE:
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    else:
        kwargs.update(
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_recycle=settings.db_pool_recycle,
        )
    return kwargs


engine: Engine = create_engine(settings.database_url, **_engine_kwargs())
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine, expire_on_commit=False)


class Base(DeclarativeBase):
    """Declarative base for every ORM model (SQLAlchemy 2.0 annotated mappings)."""


if IS_SQLITE:

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _record):  # pragma: no cover - driver hook
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA busy_timeout=8000")
        finally:
            cursor.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a scoped session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope for background workers and scripts."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def check_db_health() -> dict:
    """Cheap liveness probe used by ``/api/health/ready``."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"database": "ok", "dialect": engine.dialect.name}
    except Exception as exc:  # pragma: no cover - failure path
        return {"database": "error", "error": str(exc), "dialect": engine.dialect.name}


def migration_state() -> dict:
    """Report the Alembic revision applied to the database (best effort)."""
    try:
        with engine.connect() as conn:
            row = conn.execute(text("SELECT version_num FROM alembic_version")).fetchone()
        return {"applied": row[0] if row else None}
    except Exception:
        return {"applied": None}


#: Fixed key for the Postgres advisory lock that serialises startup migrations.
#: (api with ``--workers N`` plus a worker container all boot at the same time.)
_MIGRATION_LOCK_KEY = 8_242_017_337


def run_migrations() -> None:
    """Apply Alembic migrations up to head (no-op if the script dir is absent)."""
    from alembic import command
    from alembic.config import Config

    here = os.path.dirname(os.path.abspath(__file__))
    script_location = os.path.join(here, "..", "migrations")
    if not os.path.isdir(script_location):
        log.warning("migrations directory missing; falling back to create_all()")
        Base.metadata.create_all(bind=engine)
        return
    cfg = Config(os.path.join(here, "..", "alembic.ini"))
    cfg.set_main_option("script_location", os.path.abspath(script_location))
    cfg.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))
    if IS_SQLITE:
        command.upgrade(cfg, "head")
        return
    with engine.begin() as connection:
        connection.execute(text(f"SELECT pg_advisory_lock({_MIGRATION_LOCK_KEY})"))
        try:
            command.upgrade(cfg, "head")
        finally:
            connection.execute(text(f"SELECT pg_advisory_unlock({_MIGRATION_LOCK_KEY})"))


def stamp_migrations() -> None:
    from alembic import command
    from alembic.config import Config

    here = os.path.dirname(os.path.abspath(__file__))
    cfg = Config(os.path.join(here, "..", "alembic.ini"))
    cfg.set_main_option("script_location", os.path.abspath(os.path.join(here, "..", "migrations")))
    cfg.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))
    command.stamp(cfg, "head")


def _legacy_sqlite_migrate() -> None:
    """Add missing columns to an existing SQLite dev DB (pre-Alembic installs)."""
    if not IS_SQLITE:
        return
    insp = inspect(engine)
    from app.models import models  # noqa: F401

    for table in Base.metadata.tables.values():
        if not insp.has_table(table.name):
            continue
        existing = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name in existing:
                continue
            col_type = col.type.compile(engine.dialect)
            with engine.begin() as conn:
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {col_type}'))


def _has_any_tables() -> bool:
    """True when the database already holds application tables."""
    try:
        insp = inspect(engine)
        return bool(set(insp.get_table_names()) - {"alembic_version"})
    except Exception:  # pragma: no cover - unreachable database
        return False


def _has_alembic_version() -> bool:
    try:
        insp = inspect(engine)
        return insp.has_table("alembic_version")
    except Exception:  # pragma: no cover
        return False


def _looks_like_v1_schema() -> bool:
    """A pre-2.0 install: application tables exist but the users table does not."""
    try:
        insp = inspect(engine)
        tables = set(insp.get_table_names())
    except Exception:  # pragma: no cover
        return False
    return "users" not in tables and bool(tables & {"jobs", "resumes", "profiles", "settings"})


def claim_legacy_rows(user_id: int) -> int:
    """
    Assign tenant-less rows (created by v1.2, which had no multi-tenancy) to a
    user. Only meaningful during an upgrade; a no-op on fresh databases.
    """
    from app.models import models as m  # noqa: F401

    claimed = 0
    with SessionLocal() as db:
        for model in (m.Profile, m.Resume, m.Job, m.JobEvent, m.VaultEntry, m.Email, m.EmailEvent,
                      m.EmailOptOut, m.SettingsModel, m.ErrorLog, m.UserInputRequest, m.PipelineJob,
                      m.FundingCompany, m.AuditLog):
            if not hasattr(model, "user_id"):
                continue
            try:
                claimed += db.query(model).filter(model.user_id.is_(None)).update(
                    {model.user_id: user_id}, synchronize_session=False
                )
            except Exception as exc:  # pragma: no cover
                log.warning("could not claim %s rows: %s", model.__tablename__, exc)
        db.commit()
    if claimed:
        log.warning("claimed %s legacy row(s) for user %s (v1.2 → 2.0 upgrade)", claimed, user_id)
    return claimed


def init_db() -> None:
    """Create/migrate the schema. Safe to call on every process start."""
    from app.models import models  # noqa: F401  (registers metadata)

    if settings.auto_migrate:
        # Upgrading a v1.2 installation: tables exist without an alembic version.
        # Adopt the schema (add the new columns/tables) and stamp it at head
        # rather than replaying an initial migration that would fail.
        if _looks_like_v1_schema() and not _has_alembic_version():
            log.warning("legacy schema detected — adopting it and stamping migrations at head")
            Base.metadata.create_all(bind=engine)
            try:
                _legacy_sqlite_migrate()
            except Exception as exc:  # pragma: no cover
                log.warning("legacy column sync skipped: %s", exc)
            try:
                stamp_migrations()
            except Exception as exc:  # pragma: no cover
                log.warning("could not stamp migrations: %s", exc)
            return
        try:
            run_migrations()
            return
        except Exception as exc:
            log.warning("alembic upgrade failed (%s)", exc)
            if not IS_SQLITE and _has_any_tables():
                # create_all() would happily add half a schema and leave the
                # alembic revision unset — a silent fork that is painful to
                # unpick later. Fail loudly so a deploy stops instead.
                log.error(
                    "refusing to fall back to create_all on a populated database — "
                    "fix the migration (or back up and run `alembic stamp`) before starting"
                )
                raise
            log.warning("falling back to create_all (empty database)")

    Base.metadata.create_all(bind=engine)
    try:
        _legacy_sqlite_migrate()
    except Exception as exc:  # pragma: no cover
        log.warning("legacy sqlite column sync skipped: %s", exc)
