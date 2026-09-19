"""Multi-stage match results and user feedback (estimated fit, not probability)."""
from alembic import op
import sqlalchemy as sa

revision: str = "g7h8i9j0k1l2"
down_revision: str = "e3f4a5b6c7d8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "match_results",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("jobs.id"), nullable=False),
        sa.Column("persona_id", sa.Integer(), sa.ForeignKey("personas.id"), nullable=True),
        sa.Column("profile_id", sa.Integer(), nullable=True),
        sa.Column("profile_sha256", sa.String(64), nullable=False, server_default=""),
        sa.Column("job_description_sha256", sa.String(64), nullable=False, server_default=""),
        sa.Column("scorer", sa.String(24), nullable=False, server_default="deterministic"),
        sa.Column("scorer_version", sa.String(24), nullable=False, server_default="1.0.0"),
        sa.Column("model", sa.String(120), nullable=True),
        sa.Column("prompt_version", sa.String(60), nullable=True),
        sa.Column("score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("band", sa.String(12), nullable=False, server_default="unknown"),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("score_source", sa.String(24), nullable=False, server_default="unscored"),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("hard_filters", sa.JSON(), nullable=True),
        sa.Column("rubric", sa.JSON(), nullable=True),
        sa.Column("matched_skills", sa.JSON(), nullable=True),
        sa.Column("missing_skills", sa.JSON(), nullable=True),
        sa.Column("ai_review", sa.JSON(), nullable=True),
        sa.Column("guardrail_report", sa.JSON(), nullable=True),
        sa.Column("flags", sa.JSON(), nullable=True),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("staleness", sa.String(20), nullable=False, server_default="fresh"),
        sa.Column("pipeline_job_id", sa.Integer(), nullable=True),
        sa.Column("computed_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("superseded_by_id", sa.Integer(), sa.ForeignKey("match_results.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "user_id", "job_id", "persona_id", "profile_sha256",
            "job_description_sha256", "scorer_version",
            name="uq_match_inputs",
        ),
    )
    op.create_index("ix_match_results_user_id", "match_results", ["user_id"])
    op.create_index("ix_match_results_job_id", "match_results", ["job_id"])
    op.create_index("ix_match_user_job_current", "match_results", ["user_id", "job_id", "is_current"])
    op.create_index("ix_match_user_persona_score", "match_results", ["user_id", "persona_id", "score"])
    op.create_index("ix_match_user_staleness", "match_results", ["user_id", "staleness"])

    op.create_table(
        "match_feedback",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("jobs.id"), nullable=False),
        sa.Column("match_id", sa.Integer(), sa.ForeignKey("match_results.id"), nullable=True),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("meta", sa.JSON(), nullable=True),
        sa.Column("scorer_version", sa.String(24), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_match_feedback_id", "match_feedback", ["id"])
    op.create_index("ix_match_feedback_user_id", "match_feedback", ["user_id"])
    op.create_index("ix_match_feedback_job_id", "match_feedback", ["job_id"])
    op.create_index("ix_match_feedback_user_job", "match_feedback", ["user_id", "job_id", "created_at"])
    op.create_index("ix_match_feedback_user_kind", "match_feedback", ["user_id", "kind"])


def downgrade() -> None:
    for index in (
        "ix_match_feedback_user_kind",
        "ix_match_feedback_user_job",
        "ix_match_feedback_job_id",
        "ix_match_feedback_user_id",
        "ix_match_feedback_id",
    ):
        op.drop_index(index, table_name="match_feedback")
    op.drop_table("match_feedback")
    for index in (
        "ix_match_user_staleness",
        "ix_match_user_persona_score",
        "ix_match_user_job_current",
        "ix_match_results_job_id",
        "ix_match_results_user_id",
    ):
        op.drop_index(index, table_name="match_results")
    op.drop_table("match_results")
