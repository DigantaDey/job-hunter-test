"""resumable onboarding: sessions, resume documents, extraction attempts, events

The backend foundation for the resumable, user-facing onboarding workflow
(``docs/contracts/03`` and ``04``). Splits what the legacy synchronous
``POST /api/resume/upload`` did inside one HTTP request into durable entities:

* ``onboarding_sessions`` — one row per user; the persisted (and on every read
  reconciled) state of the journey, so a refresh, a restart or a second device
  resumes exactly where the user left off.
* ``resume_documents`` — the original upload: file preserved on disk,
  content-hashed (``UNIQUE(user_id, sha256, role)`` makes a re-upload of
  identical bytes a no-op), archived — never deleted — on replacement.
* ``resume_extractions`` — one append-only row per extraction attempt, versioned
  by ``extractor_version``, created before the AI call so a crash leaves a
  recoverable row instead of nothing.
* ``onboarding_events`` — append-only timeline (monotonic per-session
  ``sequence``) for support/debugging.
* ``profiles.source_extraction_id`` — the dedupe key that makes background
  re-processing idempotent: the extraction handler upserts on it, so duplicate
  job execution can never create a second profile row.

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-09-18

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'b8c9d0e1f2a3'
down_revision: Union[str, None] = 'a7b8c9d0e1f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _add_column(table: str, column: sa.Column) -> None:
    """Add a column unless it already exists (idempotent re-runs)."""
    bind = op.get_bind()
    existing = sa.inspect(bind).get_columns(table)
    if column.name in {row["name"] for row in existing}:
        return
    op.add_column(table, column)


def _create_table(name: str, *columns, constraints: tuple = ()) -> None:
    bind = op.get_bind()
    if name in set(sa.inspect(bind).get_table_names()):
        return
    op.create_table(name, *columns, *constraints)


def upgrade() -> None:
    # Order matters: documents → extractions → sessions → events (FK targets first).
    _create_table(
        'resume_documents',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('role', sa.String(length=20), nullable=False, server_default='master'),
        sa.Column('state', sa.String(length=20), nullable=False, server_default='stored'),
        sa.Column('filename', sa.String(length=300), nullable=False),
        sa.Column('display_name', sa.String(length=300), nullable=True, server_default=''),
        sa.Column('filepath', sa.String(length=600), nullable=False),
        sa.Column('content_type', sa.String(length=80), nullable=True, server_default='application/pdf'),
        sa.Column('size_bytes', sa.Integer(), nullable=True, server_default='0'),
        sa.Column('sha256', sa.String(length=64), nullable=False),
        sa.Column('text_sha256', sa.String(length=64), nullable=True),
        sa.Column('page_count', sa.Integer(), nullable=True),
        sa.Column('layout', sa.JSON(), nullable=True),
        sa.Column('profile_snapshot', sa.JSON(), nullable=True),
        sa.Column('tags', sa.JSON(), nullable=True),
        sa.Column('error_code', sa.String(length=40), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('archived_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'sha256', 'role', name='uq_resume_doc_user_hash_role'),
    )
    op.create_index('ix_resume_documents_id', 'resume_documents', ['id'])
    op.create_index('ix_resume_documents_user_id', 'resume_documents', ['user_id'])
    op.create_index('ix_resume_docs_user_role_state', 'resume_documents', ['user_id', 'role', 'state'])

    _create_table(
        'resume_extractions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('resume_document_id', sa.Integer(), nullable=False),
        sa.Column('attempt', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('kind', sa.String(length=24), nullable=False, server_default='profile_extraction'),
        sa.Column('state', sa.String(length=16), nullable=False, server_default='pending'),
        sa.Column('pipeline_job_id', sa.Integer(), nullable=True),
        sa.Column('extractor', sa.String(length=60), nullable=True, server_default='ai_profile_extractor'),
        sa.Column('extractor_version', sa.String(length=24), nullable=False, server_default='1.0.0'),
        sa.Column('model', sa.String(length=120), nullable=True),
        sa.Column('prompt_version', sa.String(length=60), nullable=True),
        sa.Column('profile_id', sa.Integer(), nullable=True),
        sa.Column('input_sha256', sa.String(length=64), nullable=True),
        sa.Column('output_sha256', sa.String(length=64), nullable=True),
        sa.Column('field_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('needs_review_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('guardrail_report', sa.JSON(), nullable=True),
        sa.Column('tokens', sa.JSON(), nullable=True),
        sa.Column('latency_ms', sa.Integer(), nullable=True, server_default='0'),
        sa.Column('error_code', sa.String(length=40), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('trigger', sa.String(length=16), nullable=True, server_default='user'),
        sa.Column('started_at', sa.DateTime(), nullable=True),
        sa.Column('finished_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.ForeignKeyConstraint(['resume_document_id'], ['resume_documents.id']),
        sa.ForeignKeyConstraint(['profile_id'], ['profiles.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'resume_document_id', 'attempt',
                            name='uq_extraction_user_doc_attempt'),
    )
    op.create_index('ix_resume_extractions_id', 'resume_extractions', ['id'])
    op.create_index('ix_resume_extractions_user_id', 'resume_extractions', ['user_id'])
    op.create_index('ix_resume_extractions_resume_document_id', 'resume_extractions',
                    ['resume_document_id'])
    op.create_index('ix_extractions_user_doc_state', 'resume_extractions',
                    ['user_id', 'resume_document_id', 'state'])

    _create_table(
        'onboarding_sessions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('state', sa.String(length=32), nullable=False, server_default='awaiting_resume'),
        sa.Column('state_since', sa.DateTime(), nullable=False),
        sa.Column('resume_document_id', sa.Integer(), nullable=True),
        sa.Column('extraction_id', sa.Integer(), nullable=True),
        sa.Column('pipeline_job_id', sa.Integer(), nullable=True),
        sa.Column('blocked', sa.JSON(), nullable=True),
        sa.Column('progress', sa.JSON(), nullable=True),
        sa.Column('started_at', sa.DateTime(), nullable=False),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.ForeignKeyConstraint(['resume_document_id'], ['resume_documents.id']),
        sa.ForeignKeyConstraint(['extraction_id'], ['resume_extractions.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', name='uq_onboarding_user'),
    )
    op.create_index('ix_onboarding_sessions_id', 'onboarding_sessions', ['id'])
    op.create_index('ix_onboarding_sessions_user_id', 'onboarding_sessions', ['user_id'])

    _create_table(
        'onboarding_events',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('session_id', sa.Integer(), nullable=False),
        sa.Column('sequence', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('event_type', sa.String(length=60), nullable=False),
        sa.Column('gate', sa.String(length=32), nullable=True),
        sa.Column('state_from', sa.String(length=32), nullable=True),
        sa.Column('state_to', sa.String(length=32), nullable=True),
        sa.Column('actor_type', sa.String(length=20), nullable=True, server_default='user'),
        sa.Column('actor_label', sa.String(length=120), nullable=True, server_default=''),
        sa.Column('pipeline_job_id', sa.Integer(), nullable=True),
        sa.Column('request_id', sa.String(length=64), nullable=True, server_default=''),
        sa.Column('payload', sa.JSON(), nullable=True),
        sa.Column('severity', sa.String(length=20), nullable=True, server_default='info'),
        sa.Column('message', sa.String(length=400), nullable=True, server_default=''),
        sa.Column('occurred_at', sa.DateTime(), nullable=False),
        sa.Column('recorded_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.ForeignKeyConstraint(['session_id'], ['onboarding_sessions.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'session_id', 'sequence',
                            name='uq_onboarding_event_sequence'),
    )
    op.create_index('ix_onboarding_events_id', 'onboarding_events', ['id'])
    op.create_index('ix_onboarding_events_user_id', 'onboarding_events', ['user_id'])
    op.create_index('ix_onboarding_events_session_id', 'onboarding_events', ['session_id'])
    op.create_index('ix_onboarding_events_user_time', 'onboarding_events', ['user_id', 'occurred_at'])

    # Idempotency key for background profile writes (see onboarding._profile_upsert).
    _add_column('profiles', sa.Column('source_extraction_id', sa.Integer(), nullable=True))
    bind = op.get_bind()
    existing_indexes = {ix['name'] for ix in sa.inspect(bind).get_indexes('profiles')}
    if 'ix_profiles_source_extraction_id' not in existing_indexes:
        op.create_index('ix_profiles_source_extraction_id', 'profiles', ['source_extraction_id'])


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    for table in ('onboarding_events', 'onboarding_sessions', 'resume_extractions',
                  'resume_documents'):
        op.execute(f"DROP TABLE IF EXISTS \"{table}\"" +
                   (" CASCADE" if dialect == 'postgresql' else ""))

    try:
        op.drop_index('ix_profiles_source_extraction_id', table_name='profiles')
    except Exception:  # pragma: no cover - index may not exist
        pass
    try:
        _drop = sa.inspect(bind)
        if 'source_extraction_id' in {c['name'] for c in _drop.get_columns('profiles')}:
            op.drop_column('profiles', 'source_extraction_id')
    except Exception:  # pragma: no cover - column may not exist
        pass
