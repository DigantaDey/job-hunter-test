from sqlalchemy import create_engine, text, inspect
from sqlalchemy.orm import sessionmaker, declarative_base
from app.core.config import settings
import os

# Ensure directories
os.makedirs(settings.upload_dir, exist_ok=True)
os.makedirs(settings.generated_dir, exist_ok=True)

engine = create_engine(settings.database_url, connect_args={"check_same_thread": False} if "sqlite" in settings.database_url else {})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _sqlite_migrate():
    """
    Lightweight column migration for SQLite dev databases: adds any model
    columns that don't exist yet (no-op for fresh databases). Production
    would use Alembic.
    """
    if "sqlite" not in settings.database_url:
        return
    insp = inspect(engine)
    from app.models import models  # noqa: F401
    for table in Base.metadata.tables.values():
        if not insp.has_table(table.name):
            continue
        existing = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name not in existing:
                col_type = col.type.compile(engine.dialect)
                default = "NULL"
                with engine.begin() as conn:
                    conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN "{col.name}" {col_type} DEFAULT {default}'))


def init_db():
    from app.models import models  # noqa: ensure models imported
    Base.metadata.create_all(bind=engine)
    try:
        _sqlite_migrate()
    except Exception:
        pass
