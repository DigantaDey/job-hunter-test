"""personas, scoring provenance, email job context and resume naming

Adds the user-persona subsystem plus the columns the fixed pipelines need:

* ``personas`` — one row per job-search track (Data Analyst, Data Scientist…),
  each with its own search context, preferences, learned memory and portrait.
* ``jobs.score_source`` / ``jobs.score_detail`` / ``jobs.persona_id`` — so a
  keyword-overlap estimate can never be presented as an AI verdict.
* ``emails.job_title`` / ``job_url`` / ``jd_excerpt`` / ``persona_id`` /
  ``verified`` / ``ai_used`` / ``guardrail_report`` — so the approval bucket can
  show which posting a draft was written for, and whether the address is real.
* ``resumes.display_name`` / ``persona_id`` / ``guardrail_report`` — professional
  download names and the accuracy verdict for the generated document.
* ``profiles.extraction_source`` — AI is the only supported source.

Revision ID: a1b2c3d4e5f6
Revises: 9f8b1a2c3d4e
Create Date: 2026-09-12

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = '9f8b1a2c3d4e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _add_column(table: str, column: sa.Column) -> None:
    """Add a column unless it already exists (idempotent re-runs)."""
    bind = op.get_bind()
    existing = sa.inspect(bind).get_columns(table)
    if column.name in {row["name"] for row in existing}:
        return
    op.add_column(table, column)


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())

    if "personas" not in tables:
        op.create_table(
            'personas',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('user_id', sa.Integer(), nullable=False),
            sa.Column('name', sa.String(length=120), nullable=False),
            sa.Column('target_role', sa.String(length=200), nullable=True),
            sa.Column('is_active', sa.Boolean(), nullable=False, server_default='1'),
            sa.Column('is_default', sa.Boolean(), nullable=False, server_default='0'),
            sa.Column('search_context', sa.JSON(), nullable=True),
            sa.Column('preferences', sa.JSON(), nullable=True),
            sa.Column('memory', sa.JSON(), nullable=True),
            sa.Column('portrait', sa.Text(), nullable=True),
            sa.Column('portrait_evidence', sa.JSON(), nullable=True),
            sa.Column('portrait_at', sa.DateTime(), nullable=True),
            sa.Column('stats', sa.JSON(), nullable=True),
            sa.Column('source_resume_id', sa.Integer(), nullable=True),
            sa.Column('last_used_at', sa.DateTime(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(['user_id'], ['users.id']),
            sa.ForeignKeyConstraint(['source_resume_id'], ['resumes.id']),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('user_id', 'name', name='uq_personas_user_name'),
        )
        op.create_index('ix_personas_id', 'personas', ['id'])
        op.create_index('ix_personas_user_id', 'personas', ['user_id'])
        op.create_index('ix_personas_user_active', 'personas', ['user_id', 'is_active'])

    _add_column('profiles', sa.Column('extraction_source', sa.String(length=20), nullable=True,
                                      server_default='ai'))

    _add_column('resumes', sa.Column('display_name', sa.String(length=300), nullable=True))
    _add_column('resumes', sa.Column('persona_id', sa.Integer(), nullable=True))
    _add_column('resumes', sa.Column('guardrail_report', sa.JSON(), nullable=True))

    _add_column('jobs', sa.Column('score_source', sa.String(length=20), nullable=True,
                                  server_default='pending'))
    _add_column('jobs', sa.Column('score_detail', sa.JSON(), nullable=True))
    _add_column('jobs', sa.Column('persona_id', sa.Integer(), nullable=True))

    _add_column('emails', sa.Column('job_title', sa.String(length=300), nullable=True))
    _add_column('emails', sa.Column('job_url', sa.String(length=500), nullable=True))
    _add_column('emails', sa.Column('jd_excerpt', sa.Text(), nullable=True))
    _add_column('emails', sa.Column('persona_id', sa.Integer(), nullable=True))
    _add_column('emails', sa.Column('verified', sa.Boolean(), nullable=False, server_default='0'))
    _add_column('emails', sa.Column('ai_used', sa.Boolean(), nullable=False, server_default='0'))
    _add_column('emails', sa.Column('guardrail_report', sa.JSON(), nullable=True))

    # Back-fill: existing resumes keep a readable download name.
    op.execute(
        "UPDATE resumes SET display_name = filename WHERE display_name IS NULL OR display_name = ''"
    )
    op.execute(
        "UPDATE jobs SET score_source = 'preliminary' "
        "WHERE (score_source IS NULL OR score_source = 'pending') AND score > 0"
    )
    op.execute(
        "UPDATE emails SET job_title = company WHERE job_title IS NULL OR job_title = ''"
    )


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    for table, columns in (
        ('emails', ['job_title', 'job_url', 'jd_excerpt', 'persona_id', 'verified',
                    'ai_used', 'guardrail_report']),
        ('jobs', ['score_source', 'score_detail', 'persona_id']),
        ('resumes', ['display_name', 'persona_id', 'guardrail_report']),
        ('profiles', ['extraction_source']),
    ):
        for column in columns:
            try:
                op.drop_column(table, column)
            except Exception:  # pragma: no cover - column may not exist
                pass

    if dialect == 'postgresql':
        op.execute("DROP INDEX IF EXISTS ix_personas_user_active")
        op.execute("DROP INDEX IF EXISTS ix_personas_user_id")
        op.execute("DROP INDEX IF EXISTS ix_personas_id")
    op.execute("DROP TABLE IF EXISTS personas")
