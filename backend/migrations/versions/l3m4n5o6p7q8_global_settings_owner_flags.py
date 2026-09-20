"""``global_settings`` — platform-wide, owner-controlled feature flags.

The owner/admin surface (``/admin`` in the SPA, ``/api/admin/*`` in the API)
needs somewhere to keep workspace-level switches that are *not* one tenant's
preference: ``app.services.flags`` consults these rows on every relevant code
path, and the owner console reads/writes them through ``/api/admin/flags``.

Idempotent like its siblings: an existing table is left alone, and the portable
``CURRENT_TIMESTAMP`` default covers PostgreSQL and SQLite alike.

Revision ID: l3m4n5o6p7q8
Revises: k2l3m4n5o6p7
Create Date: 2026-09-20
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "l3m4n5o6p7q8"
down_revision: Union[str, None] = "k2l3m4n5o6p7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if "global_settings" in set(sa.inspect(bind).get_table_names()):
        return
    op.create_table(
        "global_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("key", sa.String(length=80), nullable=False),
        sa.Column("value", sa.JSON(), nullable=True),
        sa.Column("updated_by", sa.Integer(), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False,
        ),
        sa.UniqueConstraint("key", name="uq_global_settings_key"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if "global_settings" in set(sa.inspect(bind).get_table_names()):
        op.drop_table("global_settings")
