"""shared job pool — cross-user discovery with a 7-day memory.

Three tables implement the v2.3 shared pool (``app.services.job_pool``):

* ``job_pool_entries`` — one row per posting any run has seen, deduplicated by
  canonical key. Deliberately **ownerless**: a posting is public market data,
  and ``expires_at`` (``last_seen_at + JOB_POOL_RETENTION_DAYS``) is what bounds
  it. The pruner deletes rather than archives.
* ``job_pool_seen`` — the (entry, user) link, and the only tenant column here.
  Carrying a real ``user_id`` FK is what makes account erasure reach the pool
  automatically: ``app.services.erasure`` derives its plan from the schema, and
  ``tests/test_deletion_integrity.py`` pins that the seed covers every owned
  table.
* ``job_pool_metrics`` — counters only, keyed by a SHA-256 of the canonical key.
  When an entry is pruned, its content is gone (no title, no description, no
  URL); what remains answers "how big was the market, how fast did it churn".
  Read exclusively by the owner console (``/api/admin/*``).

Idempotent like its siblings: an existing table is left alone, so the CI job
that runs ``alembic upgrade head`` twice on a database that already has the
tables (the ``create_all`` path used by tests and throwaway environments
creates them from the models) is a no-op, not an error.

Revision ID: m4n5o6p7q8r9
Revises: l3m4n5o6p7q8
Create Date: 2026-09-25
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "m4n5o6p7q8r9"
down_revision: Union[str, None] = "l3m4n5o6p7q8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())

    if "job_pool_entries" not in existing:
        op.create_table(
            "job_pool_entries",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("dedupe_key", sa.String(length=300), nullable=False),
            sa.Column("title", sa.String(length=300), nullable=False),
            sa.Column("company", sa.String(length=200), nullable=False),
            sa.Column("company_name_normalized", sa.String(length=200), nullable=False,
                      server_default=""),
            sa.Column("location", sa.String(length=200), nullable=True),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("url", sa.String(length=1000), nullable=True),
            sa.Column("source", sa.String(length=64), nullable=True, server_default="unknown"),
            sa.Column("external_id", sa.String(length=200), nullable=True, server_default=""),
            sa.Column("source_kind", sa.String(length=16), nullable=True, server_default=""),
            sa.Column("content_hash", sa.String(length=64), nullable=True, server_default=""),
            sa.Column("title_normalized", sa.String(length=300), nullable=True, server_default=""),
            sa.Column("extra", sa.JSON(), nullable=True),
            sa.Column("posted_at", sa.DateTime(), nullable=True),
            sa.Column("first_seen_at", sa.DateTime(), nullable=False,
                      server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("last_seen_at", sa.DateTime(), nullable=False,
                      server_default=sa.text("CURRENT_TIMESTAMP")),
            # Retention is enforced on this column: last_seen_at + retention days.
            sa.Column("expires_at", sa.DateTime(), nullable=False,
                      server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("times_seen", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("users_seen", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("expired", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("updated_at", sa.DateTime(), nullable=True,
                      server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.UniqueConstraint("dedupe_key", name="uq_job_pool_dedupe"),
        )
        op.create_index("ix_job_pool_entries_id", "job_pool_entries", ["id"], unique=False)
        op.create_index("ix_job_pool_entries_dedupe_key", "job_pool_entries", ["dedupe_key"], unique=False)
        op.create_index("ix_job_pool_expiry", "job_pool_entries", ["expires_at"], unique=False)
        op.create_index("ix_job_pool_last_seen", "job_pool_entries", ["last_seen_at"], unique=False)
        op.create_index("ix_job_pool_source", "job_pool_entries", ["source", "expires_at"], unique=False)
        op.create_index("ix_job_pool_company_norm", "job_pool_entries",
                        ["company_name_normalized"], unique=False)

    if "job_pool_seen" not in existing:
        op.create_table(
            "job_pool_seen",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("entry_id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("first_seen_at", sa.DateTime(), nullable=False,
                      server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("last_seen_at", sa.DateTime(), nullable=False,
                      server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.ForeignKeyConstraint(["entry_id"], ["job_pool_entries.id"]),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.UniqueConstraint("entry_id", "user_id", name="uq_job_pool_seen_entry_user"),
        )
        op.create_index("ix_job_pool_seen_id", "job_pool_seen", ["id"], unique=False)
        op.create_index("ix_job_pool_seen_user", "job_pool_seen", ["user_id"], unique=False)
        op.create_index("ix_job_pool_seen_entry", "job_pool_seen", ["entry_id"], unique=False)

    if "job_pool_metrics" not in existing:
        op.create_table(
            "job_pool_metrics",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("metric_key", sa.String(length=64), nullable=False),
            sa.Column("source", sa.String(length=64), nullable=True, server_default=""),
            sa.Column("company_name_normalized", sa.String(length=200), nullable=True,
                      server_default=""),
            sa.Column("reason", sa.String(length=24), nullable=False, server_default="expired"),
            sa.Column("times_seen", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("distinct_users", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("days_live", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("first_seen_at", sa.DateTime(), nullable=True),
            sa.Column("last_seen_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True,
                      server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.UniqueConstraint("metric_key", name="uq_job_pool_metric_key"),
        )
        op.create_index("ix_job_pool_metrics_id", "job_pool_metrics", ["id"], unique=False)
        op.create_index("ix_job_pool_metric_last_seen", "job_pool_metrics", ["last_seen_at"], unique=False)
        op.create_index("ix_job_pool_metric_source", "job_pool_metrics", ["source"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    if "job_pool_metrics" in existing:
        op.drop_table("job_pool_metrics")
    if "job_pool_seen" in existing:
        op.drop_table("job_pool_seen")
    if "job_pool_entries" in existing:
        op.drop_table("job_pool_entries")
