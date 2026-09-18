"""candidate profile extraction and review pipeline

Implements contract 02-candidate-profile:
- candidate_profiles: versioned document, state machine, is_current unique per (user, persona)
- profile_field_provenance: per-field provenance with path, value_hash, preview (null if sensitive), origin, sensitivity, confidence, band, evidence, extractor, ambiguity, review_status, review_required, needs_answer_for
- profile_field_history: append-only audit of who changed field and when

Also preserves original evidence snippets, marks fields as confirmed/uncertain/missing/conflicting,
distinguishes resume-derived vs user-confirmed, allows corrections, completeness calculation.

Revision ID: c1d2e3f4a5b6
Revises: f6a7b8c9d0e1
Create Date: 2026-09-18

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'c1d2e3f4a5b6'
down_revision: Union[str, None] = 'f6a7b8c9d0e1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _create_table(name: str, *columns, constraints: tuple = ()) -> None:
    bind = op.get_bind()
    if name in set(sa.inspect(bind).get_table_names()):
        return
    op.create_table(name, *columns, *constraints)


def upgrade() -> None:
    _create_table(
        'candidate_profiles',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('persona_id', sa.Integer(), nullable=True),
        sa.Column('version', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('state', sa.String(length=24), nullable=False, server_default='draft'),
        sa.Column('is_current', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('document', sa.JSON(), nullable=False, server_default='{}'),
        sa.Column('document_sha256', sa.String(length=64), nullable=False, server_default=''),
        sa.Column('source_document_id', sa.Integer(), nullable=True),
        sa.Column('source_extraction_id', sa.Integer(), nullable=True),
        sa.Column('review', sa.JSON(), nullable=True),
        sa.Column('completeness', sa.JSON(), nullable=True),
        sa.Column('extraction_source', sa.String(length=20), nullable=True, server_default='ai'),
        sa.Column('activated_at', sa.DateTime(), nullable=True),
        sa.Column('superseded_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.ForeignKeyConstraint(['persona_id'], ['personas.id']),
        sa.ForeignKeyConstraint(['source_document_id'], ['resume_documents.id']),
        sa.ForeignKeyConstraint(['source_extraction_id'], ['resume_extractions.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'persona_id', 'version', name='uq_candidate_user_persona_version'),
    )
    op.create_index('ix_candidate_profiles_id', 'candidate_profiles', ['id'])
    op.create_index('ix_candidate_profiles_user_id', 'candidate_profiles', ['user_id'])
    op.create_index('ix_candidate_user_current', 'candidate_profiles', ['user_id', 'persona_id', 'is_current'])
    op.create_index('ix_candidate_user_state', 'candidate_profiles', ['user_id', 'state'])

    _create_table(
        'profile_field_provenance',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('profile_id', sa.Integer(), nullable=False),
        sa.Column('path', sa.String(length=300), nullable=False),
        sa.Column('value_hash', sa.String(length=64), nullable=False),
        sa.Column('value_preview', sa.Text(), nullable=True),
        sa.Column('origin', sa.String(length=30), nullable=False),
        sa.Column('sensitivity', sa.String(length=20), nullable=False, server_default='internal'),
        sa.Column('confidence', sa.Float(), nullable=True),
        sa.Column('confidence_band', sa.String(length=10), nullable=False, server_default='none'),
        sa.Column('evidence', sa.JSON(), nullable=True),
        sa.Column('extractor', sa.JSON(), nullable=True),
        sa.Column('source_document_id', sa.Integer(), nullable=True),
        sa.Column('source_extraction_id', sa.Integer(), nullable=True),
        sa.Column('ambiguity', sa.String(length=30), nullable=False, server_default='none'),
        sa.Column('review_status', sa.String(length=24), nullable=False, server_default='needs_review'),
        sa.Column('review_required', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('reviewed_by', sa.Integer(), nullable=True),
        sa.Column('reviewed_at', sa.DateTime(), nullable=True),
        sa.Column('needs_answer_for', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.ForeignKeyConstraint(['profile_id'], ['candidate_profiles.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['source_document_id'], ['resume_documents.id']),
        sa.ForeignKeyConstraint(['source_extraction_id'], ['resume_extractions.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'profile_id', 'path', name='uq_provenance_user_profile_path'),
    )
    op.create_index('ix_profile_field_provenance_id', 'profile_field_provenance', ['id'])
    op.create_index('ix_profile_field_provenance_user_id', 'profile_field_provenance', ['user_id'])
    op.create_index('ix_provenance_user_profile', 'profile_field_provenance', ['user_id', 'profile_id'])
    op.create_index('ix_provenance_review_required', 'profile_field_provenance', ['review_required'])
    op.create_index('ix_provenance_path', 'profile_field_provenance', ['path'])

    _create_table(
        'profile_field_history',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('provenance_id', sa.Integer(), nullable=False),
        sa.Column('path', sa.String(length=300), nullable=False),
        sa.Column('previous_value_hash', sa.String(length=64), nullable=True),
        sa.Column('previous_value_preview', sa.Text(), nullable=True),
        sa.Column('new_value_hash', sa.String(length=64), nullable=False),
        sa.Column('new_value_preview', sa.Text(), nullable=True),
        sa.Column('origin_before', sa.String(length=30), nullable=True),
        sa.Column('origin_after', sa.String(length=30), nullable=False),
        sa.Column('review_status_before', sa.String(length=24), nullable=True),
        sa.Column('review_status_after', sa.String(length=24), nullable=False),
        sa.Column('actor_type', sa.String(length=20), nullable=False, server_default='user'),
        sa.Column('actor_id', sa.Integer(), nullable=True),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('event_id', sa.String(length=36), nullable=False),
        sa.Column('occurred_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.ForeignKeyConstraint(['provenance_id'], ['profile_field_provenance.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_profile_field_history_id', 'profile_field_history', ['id'])
    op.create_index('ix_profile_field_history_user_id', 'profile_field_history', ['user_id'])
    op.create_index('ix_field_history_user_provenance', 'profile_field_history', ['user_id', 'provenance_id'])
    op.create_index('ix_field_history_path', 'profile_field_history', ['path'])
    op.create_index('ix_field_history_occurred', 'profile_field_history', ['occurred_at'])


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    for table in ('profile_field_history', 'profile_field_provenance', 'candidate_profiles'):
        op.execute(f'DROP TABLE IF EXISTS "{table}"' + (" CASCADE" if dialect == 'postgresql' else ""))
