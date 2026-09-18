"""Job source lifecycle: timestamps, expiry, raw payload.

Adds the columns the normalized source abstraction needs on ``jobs``:

* ``first_seen_at`` / ``last_seen_at`` / ``last_verified_at``
* ``expired`` / ``expired_at``
* ``source_kind`` / ``content_hash`` / ``title_normalized``
* ``raw_payload`` — compact original source JSON, stored separately from
  the normalized columns and from ``jobs.extra``.

Revision ID: d2e3f4a5b6c7
Revises: c1d2e3f4a5b6
Create Date: 2026-09-18
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d2e3f4a5b6c7"
down_revision: Union[str, None] = "c1d2e3f4a5b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _add_column_if_missing(table: str, column: sa.Column) -> None:
    bind = op.get_bind()
    if table not in set(sa.inspect(bind).get_table_names()):
        return
    cols = {row["name"] for row in sa.inspect(bind).get_columns(table)}
    if column.name in cols:
        return
    op.add_column(table, column)


def _create_index_if_missing(name: str, table: str, columns: list) -> None:
    bind = op.get_bind()
    if table not in set(sa.inspect(bind).get_table_names()):
        return
    try:
        existing = {idx["name"] for idx in sa.inspect(bind).get_indexes(table)}
    except Exception:
        existing = set()
    if name in existing:
        return
    op.create_index(name, table, columns)


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"
    bool_false = sa.text("false") if is_pg else sa.text("0")
    json_empty = sa.text("'{}'::json") if is_pg else sa.text("'{}'")

    _add_column_if_missing("jobs", sa.Column("first_seen_at", sa.DateTime(), nullable=True))
    _add_column_if_missing("jobs", sa.Column("last_seen_at", sa.DateTime(), nullable=True))
    _add_column_if_missing("jobs", sa.Column("last_verified_at", sa.DateTime(), nullable=True))
    _add_column_if_missing(
        "jobs",
        sa.Column("expired", sa.Boolean(), nullable=False, server_default=bool_false),
    )
    _add_column_if_missing("jobs", sa.Column("expired_at", sa.DateTime(), nullable=True))
    _add_column_if_missing(
        "jobs", sa.Column("source_kind", sa.String(length=16), nullable=True, server_default="")
    )
    _add_column_if_missing(
        "jobs", sa.Column("content_hash", sa.String(length=64), nullable=True, server_default="")
    )
    _add_column_if_missing(
        "jobs", sa.Column("title_normalized", sa.String(length=300), nullable=True, server_default="")
    )
    _add_column_if_missing(
        "jobs",
        sa.Column("raw_payload", sa.JSON(), nullable=True, server_default=json_empty),
    )

    if "jobs" in set(sa.inspect(bind).get_table_names()):
        bind.execute(
            sa.text(
                "UPDATE jobs SET first_seen_at = discovered_at "
                "WHERE first_seen_at IS NULL AND discovered_at IS NOT NULL"
            )
        )
        bind.execute(
            sa.text(
                "UPDATE jobs SET last_seen_at = discovered_at "
                "WHERE last_seen_at IS NULL AND discovered_at IS NOT NULL"
            )
        )
        bind.execute(
            sa.text(
                "UPDATE jobs SET title_normalized = lower(title) "
                "WHERE (title_normalized IS NULL OR title_normalized = '') AND title IS NOT NULL"
            )
        )

    _create_index_if_missing("ix_jobs_user_content_hash", "jobs", ["user_id", "content_hash"])
    _create_index_if_missing("ix_jobs_user_last_seen", "jobs", ["user_id", "last_seen_at"])
    _create_index_if_missing("ix_jobs_user_expired", "jobs", ["user_id", "expired"])


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "jobs" not in tables:
        return
    for index in ("ix_jobs_user_expired", "ix_jobs_user_last_seen", "ix_jobs_user_content_hash"):
        try:
            op.drop_index(index, table_name="jobs")
        except Exception:
            pass
    cols = {row["name"] for row in sa.inspect(bind).get_columns("jobs")}
    for column in (
        "raw_payload",
        "title_normalized",
        "content_hash",
        "source_kind",
        "expired_at",
        "expired",
        "last_verified_at",
        "last_seen_at",
        "first_seen_at",
    ):
        if column in cols:
            op.drop_column("jobs", column)
