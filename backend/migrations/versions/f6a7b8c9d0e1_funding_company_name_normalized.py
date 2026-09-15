"""funding_companies.name_normalized — the dedupe key becomes the stored identity.

``FundingCompany`` deduped on the *normalised* name everywhere in the code
(``sync_funding_db``, the history linker, the process lookup) while the database
constraint was ``UNIQUE(user_id, name)`` on the **raw** name. "Acme Inc." and
"acme inc." therefore produced two rows the application believed were one.

This migration:

* adds ``funding_companies.name_normalized`` (strip + collapse whitespace +
  casefold — exactly :func:`app.services.funding_sources.normalize_company_name`,
  which cannot be expressed portably in SQL because of the whitespace collapse);
* backfills it from ``name``;
* collapses existing duplicates per ``(user_id, name_normalized)``, keeping the
  **oldest** row (lowest id — the one that carries the first-seen history) and
  re-pointing ``funding_scan_companies`` memberships at the survivor;
* replaces ``uq_funding_user_name`` with ``uq_funding_user_name_norm``.

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-09-15
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f6a7b8c9d0e1"
down_revision: Union[str, None] = "e5f6a7b8c9d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "funding_companies"
OLD_UQ = "uq_funding_user_name"
NEW_UQ = "uq_funding_user_name_norm"
INDEX = "ix_funding_companies_name_normalized"


def _normalize(value) -> str:
    """The app's rule, duplicated so the migration never imports the service."""
    return " ".join(str(value or "").split()).casefold()[:200]


def _constraints(bind) -> set:
    try:
        return {c.get("name") for c in sa.inspect(bind).get_unique_constraints(TABLE)}
    except Exception:  # pragma: no cover - dialects without introspection
        return set()


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if TABLE not in tables:  # pragma: no cover
        return
    columns = {row["name"] for row in inspector.get_columns(TABLE)}
    if "name_normalized" not in columns:
        op.add_column(
            TABLE,
            sa.Column("name_normalized", sa.String(length=200), nullable=False, server_default=""),
        )

    t = sa.table(
        TABLE,
        sa.column("id", sa.Integer()),
        sa.column("user_id", sa.Integer()),
        sa.column("name", sa.String()),
        sa.column("name_normalized", sa.String()),
    )

    # Backfill with the app's own normalizer.
    rows = bind.execute(sa.select(t.c.id, t.c.user_id, t.c.name).order_by(t.c.id)).fetchall()
    keepers: dict = {}
    duplicates: list = []          # (loser_id, winner_id)
    for row_id, user_id, name in rows:
        key = _normalize(name)
        bind.execute(sa.update(t).where(t.c.id == row_id).values(name_normalized=key))
        winner = keepers.get((user_id, key))
        if winner is None:
            keepers[(user_id, key)] = row_id      # lowest id = oldest = survivor
        else:
            duplicates.append((row_id, winner))

    if duplicates and "funding_scan_companies" in tables:
        memberships = sa.table(
            "funding_scan_companies",
            sa.column("id", sa.Integer()),
            sa.column("scan_id", sa.Integer()),
            sa.column("company_id", sa.Integer()),
        )
        for loser, winner in duplicates:
            # Re-point history at the surviving row, dropping memberships that
            # would collide with one the survivor already has for that scan.
            taken = {r[0] for r in bind.execute(
                sa.select(memberships.c.scan_id).where(memberships.c.company_id == winner))}
            for (mid, scan_id) in bind.execute(
                sa.select(memberships.c.id, memberships.c.scan_id)
                .where(memberships.c.company_id == loser)
            ).fetchall():
                if scan_id in taken:
                    bind.execute(sa.delete(memberships).where(memberships.c.id == mid))
                else:
                    bind.execute(sa.update(memberships).where(memberships.c.id == mid)
                                 .values(company_id=winner))
                    taken.add(scan_id)
    for loser, _winner in duplicates:
        bind.execute(sa.delete(t).where(t.c.id == loser))

    existing = _constraints(bind)
    with op.batch_alter_table(TABLE, schema=None) as batch_op:
        if OLD_UQ in existing:
            try:
                batch_op.drop_constraint(OLD_UQ, type_="unique")
            except Exception:  # pragma: no cover - SQLite may have inlined it
                pass
        if NEW_UQ not in existing:
            batch_op.create_unique_constraint(NEW_UQ, ["user_id", "name_normalized"])

    if INDEX not in {i.get("name") for i in sa.inspect(bind).get_indexes(TABLE)}:
        op.create_index(INDEX, TABLE, ["name_normalized"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if TABLE not in set(inspector.get_table_names()):  # pragma: no cover
        return
    if INDEX in {i.get("name") for i in inspector.get_indexes(TABLE)}:
        op.drop_index(INDEX, table_name=TABLE)
    existing = _constraints(bind)
    with op.batch_alter_table(TABLE, schema=None) as batch_op:
        if NEW_UQ in existing:
            try:
                batch_op.drop_constraint(NEW_UQ, type_="unique")
            except Exception:  # pragma: no cover
                pass
        if OLD_UQ not in existing:
            batch_op.create_unique_constraint(OLD_UQ, ["user_id", "name"])
    if "name_normalized" in {row["name"] for row in sa.inspect(bind).get_columns(TABLE)}:
        op.drop_column(TABLE, "name_normalized")
