"""pipeline_jobs.reclaim_count — the column the queue's crash guard writes.

v2.2 added ``PipelineJob.reclaim_count`` (how many times a row was re-queued
because its *lease* expired, as opposed to ``attempts``, which counts handler
failures). ``recover_stalled`` increments it and dead-letters a job that has
reclaim-looped ``QUEUE_MAX_RECLAIMS`` times, so a crash-looping item stops
bouncing forever.

The column landed in ``app/models/models.py`` and no migration ever created it:
every install on the Alembic path was therefore missing it, which the suite could
not see because ``tests/conftest.py`` builds its schema with ``create_all``. The
first ``INSERT`` into the queue — i.e. the first "run discovery now" — failed
with "no such column". This adds it (default 0, so existing rows are already
"never reclaimed").

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-09-16
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a7b8c9d0e1f2"
down_revision: Union[str, None] = "f6a7b8c9d0e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "pipeline_jobs" not in tables:
        return
    cols = {row["name"] for row in sa.inspect(bind).get_columns("pipeline_jobs")}
    if "reclaim_count" in cols:
        return
    op.add_column(
        "pipeline_jobs",
        sa.Column("reclaim_count", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "pipeline_jobs" not in tables:
        return
    cols = {row["name"] for row in sa.inspect(bind).get_columns("pipeline_jobs")}
    if "reclaim_count" in cols:
        op.drop_column("pipeline_jobs", "reclaim_count")
