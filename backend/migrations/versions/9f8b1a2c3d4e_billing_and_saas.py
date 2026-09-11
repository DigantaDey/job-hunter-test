"""billing and saas monetization

Revision ID: 9f8b1a2c3d4e
Revises: db63915afc60
Create Date: 2026-09-11

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '9f8b1a2c3d4e'
down_revision: Union[str, None] = 'db63915afc60'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # subscriptions
    op.create_table(
        'subscriptions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('plan', sa.String(length=20), nullable=False, server_default='free'),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='active'),
        sa.Column('provider', sa.String(length=20), nullable=False, server_default='manual'),
        sa.Column('provider_customer_id', sa.String(length=200), nullable=True),
        sa.Column('provider_subscription_id', sa.String(length=200), nullable=True),
        sa.Column('current_period_start', sa.DateTime(), nullable=True),
        sa.Column('current_period_end', sa.DateTime(), nullable=True),
        sa.Column('trial_end', sa.DateTime(), nullable=True),
        sa.Column('cancel_at_period_end', sa.Boolean(), nullable=False, server_default='0'),
        sa.Column('grace_until', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', name='uq_subscriptions_user')
    )
    op.create_index(op.f('ix_subscriptions_id'), 'subscriptions', ['id'], unique=False)
    op.create_index(op.f('ix_subscriptions_user_id'), 'subscriptions', ['user_id'], unique=False)

    # billing_events
    op.create_table(
        'billing_events',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('provider', sa.String(length=20), nullable=False),
        sa.Column('provider_event_id', sa.String(length=200), nullable=False),
        sa.Column('kind', sa.String(length=80), nullable=True),
        sa.Column('payload', sa.JSON(), nullable=True),
        sa.Column('processed', sa.Boolean(), nullable=False, server_default='0'),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('provider', 'provider_event_id', name='uq_billing_provider_event')
    )
    op.create_index(op.f('ix_billing_events_id'), 'billing_events', ['id'], unique=False)
    op.create_index(op.f('ix_billing_events_user_id'), 'billing_events', ['user_id'], unique=False)
    op.create_index('ix_billing_user_created', 'billing_events', ['user_id', 'created_at'], unique=False)

    # ai_credit_ledger
    op.create_table(
        'ai_credit_ledger',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('workflow', sa.String(length=40), nullable=False),
        sa.Column('model', sa.String(length=120), nullable=True),
        sa.Column('prompt_tokens', sa.Integer(), nullable=True),
        sa.Column('completion_tokens', sa.Integer(), nullable=True),
        sa.Column('total_tokens', sa.Integer(), nullable=True),
        sa.Column('estimated_cost_usd', sa.Float(), nullable=True),
        sa.Column('success', sa.Boolean(), nullable=False, server_default='1'),
        sa.Column('latency_ms', sa.Integer(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('meta', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_ai_credit_ledger_id'), 'ai_credit_ledger', ['id'], unique=False)
    op.create_index(op.f('ix_ai_credit_ledger_user_id'), 'ai_credit_ledger', ['user_id'], unique=False)
    op.create_index('ix_ai_ledger_user_created', 'ai_credit_ledger', ['user_id', 'created_at'], unique=False)

    # usage_counters
    op.create_table(
        'usage_counters',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('period', sa.String(length=7), nullable=False),
        sa.Column('capability', sa.String(length=40), nullable=False),
        sa.Column('count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('limit', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'period', 'capability', name='uq_usage_user_period_cap')
    )
    op.create_index(op.f('ix_usage_counters_id'), 'usage_counters', ['id'], unique=False)
    op.create_index(op.f('ix_usage_counters_user_id'), 'usage_counters', ['user_id'], unique=False)
    op.create_index('ix_usage_user_period', 'usage_counters', ['user_id', 'period'], unique=False)

    # notifications
    op.create_table(
        'notifications',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(length=40), nullable=True),
        sa.Column('title', sa.String(length=200), nullable=True),
        sa.Column('body', sa.Text(), nullable=True),
        sa.Column('link', sa.String(length=500), nullable=True),
        sa.Column('read', sa.Boolean(), nullable=False, server_default='0'),
        sa.Column('meta', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_notifications_id'), 'notifications', ['id'], unique=False)
    op.create_index(op.f('ix_notifications_user_id'), 'notifications', ['user_id'], unique=False)
    op.create_index('ix_notifications_user_read', 'notifications', ['user_id', 'read', 'created_at'], unique=False)

    # interview_preps
    op.create_table(
        'interview_preps',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('job_id', sa.Integer(), nullable=True),
        sa.Column('job_title', sa.String(length=300), nullable=True),
        sa.Column('company', sa.String(length=200), nullable=True),
        sa.Column('questions', sa.JSON(), nullable=True),
        sa.Column('answers', sa.JSON(), nullable=True),
        sa.Column('feedback', sa.JSON(), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=True, server_default='draft'),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['job_id'], ['jobs.id'], ),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_interview_preps_id'), 'interview_preps', ['id'], unique=False)
    op.create_index(op.f('ix_interview_preps_user_id'), 'interview_preps', ['user_id'], unique=False)
    op.create_index('ix_interview_user_job', 'interview_preps', ['user_id', 'job_id'], unique=False)

    # company_intel
    op.create_table(
        'company_intel',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('company', sa.String(length=200), nullable=False),
        sa.Column('website', sa.String(length=500), nullable=True),
        sa.Column('industry', sa.String(length=200), nullable=True),
        sa.Column('size', sa.String(length=50), nullable=True),
        sa.Column('funding_stage', sa.String(length=50), nullable=True),
        sa.Column('tech_stack', sa.JSON(), nullable=True),
        sa.Column('culture', sa.Text(), nullable=True),
        sa.Column('recent_news', sa.JSON(), nullable=True),
        sa.Column('sources', sa.JSON(), nullable=True),
        sa.Column('summary', sa.Text(), nullable=True),
        sa.Column('verified', sa.Boolean(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'company', name='uq_intel_user_company')
    )
    op.create_index(op.f('ix_company_intel_id'), 'company_intel', ['id'], unique=False)
    op.create_index(op.f('ix_company_intel_user_id'), 'company_intel', ['user_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_company_intel_user_id'), table_name='company_intel')
    op.drop_index(op.f('ix_company_intel_id'), table_name='company_intel')
    op.drop_table('company_intel')

    op.drop_index('ix_interview_user_job', table_name='interview_preps')
    op.drop_index(op.f('ix_interview_preps_user_id'), table_name='interview_preps')
    op.drop_index(op.f('ix_interview_preps_id'), table_name='interview_preps')
    op.drop_table('interview_preps')

    op.drop_index('ix_notifications_user_read', table_name='notifications')
    op.drop_index(op.f('ix_notifications_user_id'), table_name='notifications')
    op.drop_index(op.f('ix_notifications_id'), table_name='notifications')
    op.drop_table('notifications')

    op.drop_index('ix_usage_user_period', table_name='usage_counters')
    op.drop_index(op.f('ix_usage_counters_user_id'), table_name='usage_counters')
    op.drop_index(op.f('ix_usage_counters_id'), table_name='usage_counters')
    op.drop_table('usage_counters')

    op.drop_index('ix_ai_ledger_user_created', table_name='ai_credit_ledger')
    op.drop_index(op.f('ix_ai_credit_ledger_user_id'), table_name='ai_credit_ledger')
    op.drop_index(op.f('ix_ai_credit_ledger_id'), table_name='ai_credit_ledger')
    op.drop_table('ai_credit_ledger')

    op.drop_index('ix_billing_user_created', table_name='billing_events')
    op.drop_index(op.f('ix_billing_events_user_id'), table_name='billing_events')
    op.drop_index(op.f('ix_billing_events_id'), table_name='billing_events')
    op.drop_table('billing_events')

    op.drop_index(op.f('ix_subscriptions_user_id'), table_name='subscriptions')
    op.drop_index(op.f('ix_subscriptions_id'), table_name='subscriptions')
    op.drop_table('subscriptions')
