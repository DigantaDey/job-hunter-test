"""Search discovery cache, shared budgets, and independent usage ledger."""
import sqlalchemy as sa
from alembic import op

revision = "e3f4a5b6c7d8"
down_revision = "d2e3f4a5b6c7"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "search_cache",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("lease_until", sa.DateTime(), nullable=False),
        sa.Column("owner", sa.String(36), nullable=False),
    )
    op.create_index("ix_search_cache_expires_at", "search_cache", ["expires_at"])
    op.create_table(
        "search_budgets",
        sa.Column("key", sa.String(100), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE")),
        sa.Column("used", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_search_budgets_user_id", "search_budgets", ["user_id"])
    op.create_index("ix_search_budgets_expires_at", "search_budgets", ["expires_at"])
    op.create_table(
        "search_usage",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider", sa.String(40), nullable=False),
        sa.Column("query_hash", sa.String(64), nullable=False),
        sa.Column("requests", sa.Integer(), nullable=False),
        sa.Column("estimated_cost_microusd", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.String(24), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_search_usage_user_id", "search_usage", ["user_id"])
    op.create_index("ix_search_usage_created_at", "search_usage", ["created_at"])


def downgrade():
    for table in ("search_usage", "search_budgets", "search_cache"):
        op.drop_table(table)
