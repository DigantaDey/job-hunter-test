"""Browser-assisted application sessions — pauses, checkpoints, submission ledger.

The human-in-the-loop half of the apply flow (docs/contracts/12):

* ``application_sessions`` — one assisted run per (user, job), with the state
  machine, the per-field checkpoint (statuses + value fingerprints, never
  values), the opt-in encrypted Playwright ``storage_state``, the handoff token
  (stored hashed) and the expiry clock.
* ``application_actions`` — the queue a pause creates: login / MFA / CAPTCHA /
  unknown / ambiguous / sensitive / legal / expired / review. Unique per
  ``(user_id, dedupe_key)`` so a re-observed page bumps ``occurrences`` instead
  of stacking duplicate items.
* ``application_submissions`` — the at-most-once ledger. Unique per
  ``(user_id, idempotency_key)`` plus a partial unique index on
  ``(user_id, job_id)`` for a live (``reserved``/``submitted``/``verified``)
  row, so a resume, a retry or a replayed request cannot submit twice.

Revision ID: i9j0k1l2m3n4
Revises: h8i9j0k1l2m3
Create Date: 2026-09-19
"""

from alembic import op
import sqlalchemy as sa

revision: str = "i9j0k1l2m3n4"
down_revision: str = "h8i9j0k1l2m3"
branch_labels = None
depends_on = None

_LIVE_STATES = "state IN ('reserved', 'submitted', 'verified')"


def upgrade() -> None:
    op.create_table(
        "application_sessions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("jobs.id"), nullable=False),
        sa.Column("state", sa.String(24), nullable=False, server_default="created"),
        sa.Column("phase", sa.String(16), nullable=False, server_default="new"),
        sa.Column("state_reason", sa.String(60), nullable=True, server_default=""),
        sa.Column("actor_type", sa.String(20), nullable=False, server_default="system_api"),
        sa.Column("last_checkpoint_failure", sa.String(40), nullable=True, server_default=""),
        sa.Column("portal_type", sa.String(30), nullable=True, server_default="custom"),
        sa.Column("portal_domain", sa.String(200), nullable=True, server_default=""),
        sa.Column("isolation_key", sa.String(80), nullable=False, server_default=""),
        sa.Column("browser_profile_ref", sa.String(80), nullable=True, server_default=""),
        sa.Column("url_fingerprint", sa.String(64), nullable=False, server_default=""),
        sa.Column("expected_host", sa.String(200), nullable=False, server_default=""),
        sa.Column("employer_fingerprint", sa.String(64), nullable=False, server_default=""),
        sa.Column("application_identity", sa.String(120), nullable=False, server_default=""),
        sa.Column("checkpoint", sa.JSON(), nullable=True),
        sa.Column("progress", sa.JSON(), nullable=True),
        sa.Column("last_observation", sa.JSON(), nullable=True),
        sa.Column("fill_values", sa.JSON(), nullable=True),
        sa.Column("storage_state_enc", sa.Text(), nullable=True),
        sa.Column("storage_state_saved_at", sa.DateTime(), nullable=True),
        sa.Column("storage_state_purged_at", sa.DateTime(), nullable=True),
        sa.Column("storage_state_expires_at", sa.DateTime(), nullable=True),
        sa.Column("handoff_token_hash", sa.String(64), nullable=True),
        sa.Column("handoff_expires_at", sa.DateTime(), nullable=True),
        sa.Column("handoff_action_id", sa.Integer(), nullable=True),
        sa.Column("screenshots", sa.JSON(), nullable=True),
        sa.Column("pause_kind", sa.String(30), nullable=True, server_default=""),
        sa.Column("pause_reason", sa.String(80), nullable=True, server_default=""),
        sa.Column("resumed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_activity_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_application_sessions_id", "application_sessions", ["id"])
    op.create_index("ix_application_sessions_user_id", "application_sessions", ["user_id"])
    op.create_index("ix_application_sessions_job_id", "application_sessions", ["job_id"])
    op.create_index("ix_app_sessions_user_state", "application_sessions", ["user_id", "state"])
    op.create_index("ix_app_sessions_user_job", "application_sessions", ["user_id", "job_id"])
    op.create_index("ix_app_sessions_job_live", "application_sessions", ["job_id", "state"])

    op.create_table(
        "application_actions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("jobs.id"), nullable=True),
        sa.Column("session_id", sa.Integer(), sa.ForeignKey("application_sessions.id"), nullable=True),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("reason", sa.String(60), nullable=True, server_default=""),
        sa.Column("title", sa.String(200), nullable=True, server_default=""),
        sa.Column("instructions", sa.Text(), nullable=True, server_default=""),
        sa.Column("fields", sa.JSON(), nullable=True),
        sa.Column("handoff", sa.JSON(), nullable=True),
        sa.Column("dedupe_key", sa.String(200), nullable=False),
        sa.Column("occurrences", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("user_id", "dedupe_key", name="uq_application_actions_dedupe"),
    )
    op.create_index("ix_application_actions_id", "application_actions", ["id"])
    op.create_index("ix_application_actions_user_id", "application_actions", ["user_id"])
    op.create_index("ix_application_actions_job_id", "application_actions", ["job_id"])
    op.create_index("ix_application_actions_session_id", "application_actions", ["session_id"])
    op.create_index("ix_app_actions_user_status", "application_actions", ["user_id", "status"])
    op.create_index("ix_app_actions_session", "application_actions", ["session_id", "status"])

    op.create_table(
        "application_submissions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("jobs.id"), nullable=False),
        sa.Column("session_id", sa.Integer(), sa.ForeignKey("application_sessions.id"), nullable=True),
        sa.Column("state", sa.String(16), nullable=False, server_default="reserved"),
        sa.Column("channel", sa.String(20), nullable=False, server_default="assisted_dry_run"),
        sa.Column("idempotency_key", sa.String(120), nullable=False),
        sa.Column("dry_run", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("refusal_reason", sa.String(40), nullable=True, server_default=""),
        sa.Column("receipt", sa.JSON(), nullable=True),
        sa.Column("reserved_at", sa.DateTime(), nullable=True),
        sa.Column("submitted_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("user_id", "idempotency_key", name="uq_application_submissions_key"),
    )
    op.create_index("ix_application_submissions_id", "application_submissions", ["id"])
    op.create_index("ix_application_submissions_user_id", "application_submissions", ["user_id"])
    op.create_index("ix_application_submissions_job_id", "application_submissions", ["job_id"])
    op.create_index("ix_application_submissions_user_job", "application_submissions",
                    ["user_id", "job_id"])
    op.create_index(
        "uq_application_submissions_live_job",
        "application_submissions",
        ["user_id", "job_id"],
        unique=True,
        sqlite_where=sa.text(_LIVE_STATES),
        postgresql_where=sa.text(_LIVE_STATES),
    )


def downgrade() -> None:
    for idx in (
        "uq_application_submissions_live_job",
        "ix_application_submissions_user_job",
        "ix_application_submissions_job_id",
        "ix_application_submissions_user_id",
        "ix_application_submissions_id",
    ):
        try:
            op.drop_index(idx, table_name="application_submissions")
        except Exception:
            pass
    try:
        op.drop_table("application_submissions")
    except Exception:
        pass
    for idx in (
        "ix_app_actions_session",
        "ix_app_actions_user_status",
        "ix_application_actions_session_id",
        "ix_application_actions_job_id",
        "ix_application_actions_user_id",
        "ix_application_actions_id",
    ):
        try:
            op.drop_index(idx, table_name="application_actions")
        except Exception:
            pass
    try:
        op.drop_table("application_actions")
    except Exception:
        pass
    for idx in (
        "ix_app_sessions_job_live",
        "ix_app_sessions_user_job",
        "ix_app_sessions_user_state",
        "ix_application_sessions_job_id",
        "ix_application_sessions_user_id",
        "ix_application_sessions_id",
    ):
        try:
            op.drop_index(idx, table_name="application_sessions")
        except Exception:
            pass
    try:
        op.drop_table("application_sessions")
    except Exception:
        pass
