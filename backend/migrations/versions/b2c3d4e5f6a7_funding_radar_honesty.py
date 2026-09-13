"""funding radar: real freshness clocks, drop the dead hiring flag

* ``funding_companies.last_seen_at`` — the most recent scan that still returned
  the company. ``discovered_at`` is now **first seen** (it used to be rewritten
  on every sync, so a company that providers kept returning was never pruned
  and "discovered 2 minutes ago" was meaningless). Prune runs on
  ``COALESCE(last_seen_at, discovered_at)``.
* ``ix_funding_user_seen`` — the ``(user_id, last_seen_at)`` index the prune
  query needs; it used to load every row for the user and delete them one by
  one.
* drops ``funding_companies.has_open_positions`` — a flag no code path ever set
  to ``True``. The API returned it as though it were a fact (the UI rendered an
  "open roles" badge), and the apply-flow branch it gated was unreachable: it
  scored an invented job description instead. Real open positions, when a
  provider reports them, live in ``meta.open_positions``.
* collapses the case-variant duplicates the old upsert created ("Stripe" and
  "stripe" were two rows for one user), keeping the earliest of each group —
  the one that carries the first-seen history.

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-09-13

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = 'funding_companies'
INDEX = 'ix_funding_user_seen'


def _columns() -> set:
    return {row["name"] for row in sa.inspect(op.get_bind()).get_columns(TABLE)}


def _indexes() -> set:
    return {row.get("name") for row in sa.inspect(op.get_bind()).get_indexes(TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    if TABLE not in set(sa.inspect(bind).get_table_names()):  # pragma: no cover
        return

    columns = _columns()
    if "last_seen_at" not in columns:
        op.add_column(TABLE, sa.Column('last_seen_at', sa.DateTime(), nullable=True))
    # Back-fill: an existing row was "seen" when it was last written.
    op.execute(f"UPDATE {TABLE} SET last_seen_at = discovered_at WHERE last_seen_at IS NULL")
    op.execute(f"UPDATE {TABLE} SET last_seen_at = raised_at WHERE last_seen_at IS NULL")

    if "has_open_positions" in _columns():
        # Collapse the duplicates the case-sensitive upsert created ("Stripe" and
        # "stripe" were two rows for one user): keep the earliest row of each
        # group, which is the one that carries the first-seen history.
        op.execute(
            f"DELETE FROM {TABLE} WHERE id NOT IN "
            f"(SELECT MIN(id) FROM {TABLE} GROUP BY user_id, lower(trim(name)))"
        )
        # SQLite >= 3.35 and PostgreSQL both support DROP COLUMN natively.
        op.drop_column(TABLE, 'has_open_positions')

    if INDEX not in _indexes():
        op.create_index(INDEX, TABLE, ['user_id', 'last_seen_at'], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    if TABLE not in set(sa.inspect(bind).get_table_names()):  # pragma: no cover
        return

    columns = _columns()
    if "has_open_positions" not in columns:
        op.add_column(TABLE, sa.Column('has_open_positions', sa.Boolean(), nullable=True))
    if INDEX in _indexes():
        op.drop_index(INDEX, table_name=TABLE)
    if "last_seen_at" in _columns():
        op.drop_column(TABLE, 'last_seen_at')
