"""Application packets — reviewable, versioned artifacts per job (no auto-submit).

One packet per (user, job, version) carries every preparation artifact:
tailored resume, cover note, short answers, outreach draft, checklist,
summary, evidence, emphasized facts, JD version, guardrail report and token
usage. The master profile is snapshotted and never mutated; old packets are
superseded but retained. Status is the approval gate (pending_approval →
approved/rejected). Hallucination is blocked by FactLedger + schema checks
before persistence.

Revision ID: h8i9j0k1l2m3
Revises: g7h8i9j0k1l2
Create Date: 2026-09-19
"""

from alembic import op
import sqlalchemy as sa

revision: str = "h8i9j0k1l2m3"
down_revision: str = "g7h8i9j0k1l2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "application_packets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("jobs.id"), nullable=False),
        sa.Column("persona_id", sa.Integer(), sa.ForeignKey("personas.id"), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(24), nullable=False, server_default="pending_approval"),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("jd_hash", sa.String(64), nullable=False, server_default=""),
        sa.Column("jd_text_snapshot", sa.Text(), nullable=True, server_default=""),
        sa.Column("jd_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("master_profile_snapshot", sa.JSON(), nullable=True),
        sa.Column("master_profile_id", sa.Integer(), nullable=True),
        sa.Column("tailored_resume", sa.JSON(), nullable=True),
        sa.Column("cover_note", sa.Text(), nullable=True, server_default=""),
        sa.Column("short_answers", sa.JSON(), nullable=True),
        sa.Column("outreach_draft", sa.JSON(), nullable=True),
        sa.Column("checklist", sa.JSON(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True, server_default=""),
        sa.Column("evidence", sa.JSON(), nullable=True),
        sa.Column("emphasized_facts", sa.JSON(), nullable=True),
        sa.Column("guardrail_report", sa.JSON(), nullable=True),
        sa.Column("token_usage", sa.JSON(), nullable=True),
        sa.Column("job_title", sa.String(300), nullable=True, server_default=""),
        sa.Column("company", sa.String(200), nullable=True, server_default=""),
        sa.Column("generated_at", sa.DateTime(), nullable=False),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.Column("rejected_at", sa.DateTime(), nullable=True),
        sa.Column("superseded_at", sa.DateTime(), nullable=True),
        sa.Column("reviewed_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("user_id", "job_id", "version", name="uq_packet_user_job_version"),
    )
    op.create_index("ix_application_packets_user_id", "application_packets", ["user_id"])
    op.create_index("ix_application_packets_job_id", "application_packets", ["job_id"])
    op.create_index("ix_packet_user_job_current", "application_packets", ["user_id", "job_id", "is_current"])
    op.create_index("ix_packet_user_status", "application_packets", ["user_id", "status"])
    op.create_index("ix_packet_user_job", "application_packets", ["user_id", "job_id"])

    op.create_table(
        "application_packet_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("packet_id", sa.Integer(), sa.ForeignKey("application_packets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("jobs.id"), nullable=True),
        sa.Column("event_type", sa.String(40), nullable=False),
        sa.Column("from_status", sa.String(24), nullable=True),
        sa.Column("to_status", sa.String(24), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True, server_default=""),
        sa.Column("meta", sa.JSON(), nullable=True),
        sa.Column("actor_type", sa.String(20), nullable=False, server_default="user"),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_application_packet_events_packet_id", "application_packet_events", ["packet_id"])
    op.create_index("ix_application_packet_events_user_id", "application_packet_events", ["user_id"])
    op.create_index("ix_packet_events_packet_time", "application_packet_events", ["packet_id", "occurred_at"])
    op.create_index("ix_packet_events_user", "application_packet_events", ["user_id", "occurred_at"])


def downgrade() -> None:
    for idx in (
        "ix_packet_events_user",
        "ix_packet_events_packet_time",
        "ix_application_packet_events_user_id",
        "ix_application_packet_events_packet_id",
    ):
        try:
            op.drop_index(idx, table_name="application_packet_events")
        except Exception:
            pass
    try:
        op.drop_table("application_packet_events")
    except Exception:
        pass
    for idx in (
        "ix_packet_user_job",
        "ix_packet_user_status",
        "ix_packet_user_job_current",
        "ix_application_packets_job_id",
        "ix_application_packets_user_id",
    ):
        try:
            op.drop_index(idx, table_name="application_packets")
        except Exception:
            pass
    try:
        op.drop_table("application_packets")
    except Exception:
        pass
