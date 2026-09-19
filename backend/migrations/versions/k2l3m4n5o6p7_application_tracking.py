"""Application tracking — the lifecycle & outcome-feedback layer (v1, lightweight).

``docs/contracts/07-application-state-machine.md`` §11 and
``docs/contracts/09-application-events.md`` §8. Adds:

* ``application_tracking`` — one row per ``(user_id, job_id)``: the **current**
  outcome state as a projection, the origin of that state (system-observed vs
  user-reported), the follow-up promise, and the snapshots a later report needs
  (source attribution, role family, match score + scorer version, artefact kind +
  version + hash). The snapshot ids (``match_id``, ``artifact_id``) are
  deliberately **not** FKs: a snapshot must outlive the artefact it points at,
  which is the ``automation_policy_revisions.policy_id`` pattern.
* ``application_tracking_events`` — the append-only timeline. Gapless
  ``sequence`` per record (unique with ``user_id``), a UUID ``event_id`` for
  cross-process dedupe, a nullable ``dedupe_key`` with its own unique index (NULLs
  are distinct on both SQLite and PostgreSQL, so only rows that *asked* to be
  deduped collide), the mandatory ``actor_type``/``origin``, ``occurred_at`` vs
  ``recorded_at``, and ``is_correction``/``correction_of_event_id`` — a manual
  correction adds a row, it never edits one.

Backfill: jobs the board already shows as ``applied`` get a record and one
``application.tracking_opened`` event with ``trigger = 'recovery'`` and
``payload.backfilled = true``, because "the board said applied" is a fact we
observed and not a fact the user just told us. Nothing is invented: no interview,
no outcome, no source we do not already store. The pass is idempotent
(``NOT EXISTS``) so a re-run inserts nothing.

Revision ID: k2l3m4n5o6p7
Revises: j1k2l3m4n5o6
Create Date: 2026-09-19
"""

from alembic import op
import sqlalchemy as sa

revision: str = "k2l3m4n5o6p7"
down_revision: str = "j1k2l3m4n5o6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- the outcome record ------------------------------------------------- #
    op.create_table(
        "application_tracking",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("jobs.id"), nullable=False),
        sa.Column("persona_id", sa.Integer(), sa.ForeignKey("personas.id"), nullable=True),
        sa.Column("state", sa.String(28), nullable=False, server_default="not_applied"),
        sa.Column("phase", sa.String(20), nullable=False, server_default="pre_application"),
        sa.Column("state_origin", sa.String(24), nullable=False, server_default="user_reported"),
        sa.Column("state_actor_type", sa.String(20), nullable=False, server_default="user"),
        sa.Column("is_provisional", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("provisional_evidence", sa.JSON(), nullable=True),
        # attribution & snapshots
        sa.Column("source", sa.String(60), nullable=False, server_default="unknown"),
        sa.Column("channel", sa.String(20), nullable=True, server_default=""),
        sa.Column("role_family", sa.String(80), nullable=True, server_default=""),
        sa.Column("job_title_snapshot", sa.String(300), nullable=True, server_default=""),
        sa.Column("company_snapshot", sa.String(200), nullable=True, server_default=""),
        sa.Column("match_score", sa.Float(), nullable=True),
        sa.Column("match_band", sa.String(12), nullable=True, server_default=""),
        sa.Column("match_id", sa.Integer(), nullable=True),
        sa.Column("score_source", sa.String(24), nullable=True, server_default=""),
        sa.Column("scorer_version", sa.String(24), nullable=True, server_default=""),
        sa.Column("artifact_kind", sa.String(20), nullable=False, server_default="none"),
        sa.Column("artifact_id", sa.Integer(), nullable=True),
        sa.Column("artifact_version", sa.Integer(), nullable=True),
        sa.Column("artifact_label", sa.String(200), nullable=True, server_default=""),
        sa.Column("artifact_sha256", sa.String(64), nullable=True, server_default=""),
        # business timestamps
        sa.Column("applied_at", sa.DateTime(), nullable=True),
        sa.Column("first_response_at", sa.DateTime(), nullable=True),
        sa.Column("interview_scheduled_at", sa.DateTime(), nullable=True),
        sa.Column("interview_at", sa.DateTime(), nullable=True),
        sa.Column("interview_completed_at", sa.DateTime(), nullable=True),
        sa.Column("outcome_at", sa.DateTime(), nullable=True),
        sa.Column("closed_at", sa.DateTime(), nullable=True),
        # the follow-up promise
        sa.Column("follow_up_at", sa.DateTime(), nullable=True),
        sa.Column("follow_up_note", sa.String(400), nullable=True, server_default=""),
        sa.Column("follow_up_notified_at", sa.DateTime(), nullable=True),
        sa.Column("follow_up_completed_at", sa.DateTime(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("last_sequence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("event_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("correction_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("applied_idempotency_keys", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("user_id", "job_id", name="uq_application_tracking_user_job"),
    )
    op.create_index("ix_application_tracking_id", "application_tracking", ["id"])
    op.create_index("ix_application_tracking_user_id", "application_tracking", ["user_id"])
    op.create_index("ix_application_tracking_job_id", "application_tracking", ["job_id"])
    op.create_index("ix_app_tracking_user_state", "application_tracking", ["user_id", "state"])
    op.create_index("ix_app_tracking_user_source", "application_tracking", ["user_id", "source"])
    op.create_index("ix_app_tracking_user_applied", "application_tracking", ["user_id", "applied_at"])
    op.create_index("ix_app_tracking_user_follow_up", "application_tracking", ["user_id", "follow_up_at"])
    op.create_index("ix_app_tracking_user_artifact", "application_tracking",
                    ["user_id", "artifact_kind", "artifact_version"])

    # --- the append-only timeline ------------------------------------------- #
    op.create_table(
        "application_tracking_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("event_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("tracking_id", sa.Integer(),
                  sa.ForeignKey("application_tracking.id", ondelete="CASCADE"), nullable=False),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("jobs.id"), nullable=True),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(60), nullable=False),
        sa.Column("state_from", sa.String(28), nullable=True),
        sa.Column("state_to", sa.String(28), nullable=True),
        sa.Column("phase", sa.String(20), nullable=True),
        sa.Column("origin", sa.String(24), nullable=False, server_default="user_reported"),
        sa.Column("actor_type", sa.String(20), nullable=False, server_default="user"),
        sa.Column("actor_id", sa.Integer(), nullable=True),
        sa.Column("actor_label", sa.String(320), nullable=False, server_default=""),
        sa.Column("trigger", sa.String(16), nullable=False, server_default="user"),
        sa.Column("is_correction", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("correction_of_event_id", sa.Integer(), nullable=True),
        sa.Column("correction_reason", sa.String(400), nullable=True, server_default=""),
        sa.Column("is_provisional", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("severity", sa.String(20), nullable=False, server_default="info"),
        sa.Column("message", sa.String(400), nullable=False, server_default=""),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("evidence", sa.JSON(), nullable=True),
        sa.Column("follow_up_at", sa.DateTime(), nullable=True),
        sa.Column("dedupe_key", sa.String(200), nullable=True),
        sa.Column("idempotency_key", sa.String(64), nullable=True),
        sa.Column("request_id", sa.String(64), nullable=True),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("user_id", "tracking_id", "sequence",
                            name="uq_app_tracking_event_sequence"),
        sa.UniqueConstraint("user_id", "event_id", name="uq_app_tracking_event_uuid"),
        sa.UniqueConstraint("user_id", "dedupe_key", name="uq_app_tracking_event_dedupe"),
    )
    op.create_index("ix_application_tracking_events_id", "application_tracking_events", ["id"])
    op.create_index("ix_application_tracking_events_user_id", "application_tracking_events", ["user_id"])
    op.create_index("ix_application_tracking_events_tracking_id", "application_tracking_events",
                    ["tracking_id"])
    op.create_index("ix_application_tracking_events_job_id", "application_tracking_events", ["job_id"])
    op.create_index("ix_app_tracking_events_user_type", "application_tracking_events",
                    ["user_id", "event_type", "occurred_at"])
    op.create_index("ix_app_tracking_events_job_time", "application_tracking_events",
                    ["user_id", "job_id", "occurred_at"])
    op.create_index("ix_app_tracking_events_follow_up", "application_tracking_events",
                    ["user_id", "follow_up_at"])

    _backfill_applied_jobs()


def _backfill_applied_jobs() -> None:
    """Give every job the board already shows as ``applied`` a tracking record.

    Portable SQL only (no JSON extraction, no dialect-specific upsert): insert
    where no record exists yet, then one timeline row per new record. The origin
    is ``system_observed`` because that is literally what happened — the system
    observed a board status — and ``payload.backfilled`` says so, following the
    rule ``docs/contracts/09`` §5 sets for recovered rows: a guess dressed as a
    fact is what this contract set exists to prevent.
    """
    op.execute(
        """
        INSERT INTO application_tracking (
            user_id, job_id, persona_id, state, phase, state_origin, state_actor_type,
            is_provisional, source, role_family, job_title_snapshot, company_snapshot,
            match_score, score_source, artifact_kind, applied_at,
            last_sequence, event_count, correction_count, created_at, updated_at
        )
        SELECT j.user_id, j.id, j.persona_id, 'applied', 'applied', 'system_observed', 'system_api',
               false, COALESCE(NULLIF(j.source, ''), 'unknown'), '', COALESCE(j.title, ''),
               COALESCE(j.company, ''), j.score, COALESCE(j.score_source, ''), 'none',
               COALESCE(j.applied_at, j.discovered_at, CURRENT_TIMESTAMP),
               1, 1, 0, COALESCE(j.applied_at, j.discovered_at, CURRENT_TIMESTAMP),
               COALESCE(j.applied_at, j.discovered_at, CURRENT_TIMESTAMP)
        FROM jobs j
        WHERE j.status = 'applied'
          AND NOT EXISTS (
              SELECT 1 FROM application_tracking t
              WHERE t.user_id = j.user_id AND t.job_id = j.id
          )
        """
    )
    # The timeline row. ``event_id`` is generated per row: a UUID where the
    # dialect has one, otherwise the unique (tracking_id) rendered as a string —
    # both satisfy the (user_id, event_id) unique index and neither can collide
    # with a row the application writes later (those are UUIDv4).
    dialect = op.get_bind().dialect.name
    uuid_expr = (
        "gen_random_uuid()::text" if dialect == "postgresql"
        else "lower(hex(randomblob(4)) || '-' || hex(randomblob(2)) || '-4' || "
             "substr(hex(randomblob(2)),2) || '-' || substr('89ab', 1 + (abs(random()) % 4), 1) || "
             "substr(hex(randomblob(2)),2) || '-' || hex(randomblob(6)))"
    )
    op.execute(
        f"""
        INSERT INTO application_tracking_events (
            event_id, user_id, tracking_id, job_id, sequence, event_type,
            state_from, state_to, phase, origin, actor_type, actor_label, trigger,
            is_correction, is_provisional, severity, message, payload,
            occurred_at, recorded_at
        )
        SELECT {uuid_expr}, t.user_id, t.id, t.job_id, 1, 'application.tracking_opened',
               NULL, 'applied', 'applied', 'system_observed', 'system_api', 'system', 'recovery',
               false, false, 'info',
               'Tracking opened from the board status: this job was already marked applied.',
               '{{"backfilled": true, "backfill_source": "jobs.status", "state_to": "applied"}}',
               COALESCE(t.applied_at, t.created_at, CURRENT_TIMESTAMP),
               COALESCE(t.applied_at, t.created_at, CURRENT_TIMESTAMP)
        FROM application_tracking t
        WHERE t.state_origin = 'system_observed'
          AND t.last_sequence = 1
          AND NOT EXISTS (
              SELECT 1 FROM application_tracking_events e
              WHERE e.tracking_id = t.id AND e.user_id = t.user_id
          )
        """
    )


def downgrade() -> None:
    for idx in (
        "ix_app_tracking_events_follow_up",
        "ix_app_tracking_events_job_time",
        "ix_app_tracking_events_user_type",
        "ix_application_tracking_events_job_id",
        "ix_application_tracking_events_tracking_id",
        "ix_application_tracking_events_user_id",
        "ix_application_tracking_events_id",
    ):
        try:
            op.drop_index(idx, table_name="application_tracking_events")
        except Exception:  # pragma: no cover - a partial downgrade must still proceed
            pass
    try:
        op.drop_table("application_tracking_events")
    except Exception:  # pragma: no cover
        pass

    for idx in (
        "ix_app_tracking_user_artifact",
        "ix_app_tracking_user_follow_up",
        "ix_app_tracking_user_applied",
        "ix_app_tracking_user_source",
        "ix_app_tracking_user_state",
        "ix_application_tracking_job_id",
        "ix_application_tracking_user_id",
        "ix_application_tracking_id",
    ):
        try:
            op.drop_index(idx, table_name="application_tracking")
        except Exception:  # pragma: no cover
            pass
    try:
        op.drop_table("application_tracking")
    except Exception:  # pragma: no cover
        pass
