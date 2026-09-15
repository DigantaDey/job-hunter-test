"""jobs.company_name_normalized — SQL-side company filter + backfill.

``GET /api/jobs?company=`` used to load *every* row matching the other
filters (``.all()``) and filter in Python by a normalized company name,
applying LIMIT/OFFSET only afterwards — a Pro+ board of 20k rows was
transferred in full on every company-filtered page. The normalized identity
is now a column:

* ``jobs.company_name_normalized`` — lowercased, punctuation-stripped,
  legal-suffix-stripped (the exact rules of
  :func:`app.services.company_normalize.normalize_company_name`, which
  cannot be expressed portably in SQL).
* backfilled from ``jobs.company`` with the app's own normalizer.
* composite ``(user_id, company_name_normalized)`` index for the
  tenant-scoped exact-match filter.

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-09-15
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e5f6a7b8c9d0"
down_revision: Union[str, None] = "d4e5f6a7b8c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "jobs" not in tables:
        return
    cols = {row["name"] for row in sa.inspect(bind).get_columns("jobs")}
    if "company_name_normalized" in cols:
        return

    op.add_column(
        "jobs",
        sa.Column(
            "company_name_normalized",
            sa.String(length=200),
            nullable=False,
            server_default="",
        ),
    )

    # Backfill with the app's own normalizer — the same function the filter
    # applies at query time, so a row upgraded in place matches a query that
    # normalizes the input the identical way (suffix stripping is Python, not
    # SQL, which is exactly why the old code filtered in Python).
    from app.services.company_normalize import normalize_company_name

    jobs_t = sa.table(
        "jobs",
        sa.column("id", sa.Integer()),
        sa.column("company", sa.String()),
        sa.column("company_name_normalized", sa.String()),
    )
    rows = bind.execute(sa.select(jobs_t.c.id, jobs_t.c.company)).fetchall()
    for row in rows:
        norm = normalize_company_name(row[1])[:200]
        bind.execute(
            sa.update(jobs_t).where(jobs_t.c.id == row[0]).values(company_name_normalized=norm)
        )

    op.create_index(
        "ix_jobs_user_company_norm",
        "jobs",
        ["user_id", "company_name_normalized"],
        unique=False,
    )


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "jobs" not in tables:
        return
    try:
        op.drop_index("ix_jobs_user_company_norm", table_name="jobs")
    except Exception:
        pass
    cols = {row["name"] for row in sa.inspect(bind).get_columns("jobs")}
    if "company_name_normalized" in cols:
        op.drop_column("jobs", "company_name_normalized")
