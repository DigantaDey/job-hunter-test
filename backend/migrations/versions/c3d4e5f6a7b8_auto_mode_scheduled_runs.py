"""auto-mode scheduler: the ``scheduled_runs`` history/clock table

Pro / Pro+ have promised "premium automation" since v2.0, and the entitlement
flag (``can_use_scheduled_workflows``), the ``automation_runs_per_month`` quota
and the Dashboard's used/limit counter were all wired to nothing: there was no
scheduler, so the counter could never move. v2.2 adds the scheduler
(``app/services/auto_scheduler.py``, spawned as a worker child task) and this
table, which is its clock, its idempotency guard and the history the Settings
card and ``GET /api/automation`` render.

Deliberately *not* changed: ``pipeline_jobs`` (the work itself stays a normal
durable queue item, so pause/resume/retry/dead-letter semantics are unchanged)
and the usage counters (auto runs consume ``automation_runs_per_month`` exactly
where a manual run does — on completion, from the worker).

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-09-13

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'c3d4e5f6a7b8'
down_revision: Union[str, None] = 'b2c3d4e5f6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "scheduled_runs"


def upgrade() -> None:
    bind = op.get_bind()
    if TABLE in set(sa.inspect(bind).get_table_names()):  # idempotent re-run
        return
    op.create_table(
        TABLE,
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('workflow', sa.String(length=40), nullable=False),
        # The cadence window this decision belongs to (epoch seconds // cadence):
        # two sweeps inside one window must not enqueue the same work twice.
        sa.Column('cycle_bucket', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('triggered_at', sa.DateTime(), nullable=False),
        sa.Column('queue_job_id', sa.Integer(), nullable=True),
        sa.Column('job_id', sa.Integer(), nullable=True),
        sa.Column('state', sa.String(length=30), nullable=False, server_default='queued'),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('meta', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_scheduled_runs_id', TABLE, ['id'], unique=False)
    op.create_index('ix_scheduled_runs_user_id', TABLE, ['user_id'], unique=False)
    # The due check: newest completed run per (user, workflow), by time.
    op.create_index('ix_scheduled_runs_user_workflow', TABLE, ['user_id', 'workflow', 'triggered_at'], unique=False)
    # The per-window idempotency guard (checked in SQL, not enforced: a skip may
    # legitimately be followed by a real run in the same window).
    op.create_index('ix_scheduled_runs_user_bucket', TABLE, ['user_id', 'workflow', 'cycle_bucket'], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    if TABLE not in set(sa.inspect(bind).get_table_names()):  # pragma: no cover
        return
    op.drop_index('ix_scheduled_runs_user_bucket', table_name=TABLE)
    op.drop_index('ix_scheduled_runs_user_workflow', table_name=TABLE)
    op.drop_index('ix_scheduled_runs_user_id', table_name=TABLE)
    op.drop_index('ix_scheduled_runs_id', table_name=TABLE)
    op.drop_table(TABLE)
