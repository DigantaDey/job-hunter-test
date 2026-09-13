"""funding scan history + hiring linkage (v2.2.5)

* funding_scans — one row per completed scan (ok | scan_failed), with provider
  errors, counters, and meta (what the API actually returned).
* funding_scan_companies — membership: which companies a particular scan
  returned, with rank/why.
* has_open_positions on funding_companies — re-added (v2.2.4 dropped it because
  no code path set it; v2.2.5 keeps it fresh both directions via the shared
  matcher).
*Retention: after each persist, keep 30 newest scans per user and prune older.

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-09-13
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, None] = "c3d4e5f6a7b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())

    if "funding_scans" not in tables:
        op.create_table(
            "funding_scans",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("scanned_at", sa.DateTime(), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="ok"),
            sa.Column("provider_errors", sa.JSON(), nullable=True),
            sa.Column("events_seen", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("companies_found", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("meta", sa.JSON(), nullable=True),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_funding_scans_id", "funding_scans", ["id"], unique=False)
        op.create_index("ix_funding_scans_user_id", "funding_scans", ["user_id"], unique=False)
        op.create_index("ix_funding_scans_user_scanned", "funding_scans", ["user_id", "scanned_at"], unique=False)

    if "funding_scan_companies" not in tables:
        op.create_table(
            "funding_scan_companies",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("scan_id", sa.Integer(), nullable=False),
            sa.Column("company_id", sa.Integer(), nullable=False),
            sa.Column("rank", sa.Integer(), nullable=False),
            sa.Column("why", sa.Text(), nullable=True),
            sa.ForeignKeyConstraint(["scan_id"], ["funding_scans.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["company_id"], ["funding_companies.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("scan_id", "company_id", name="uq_scan_company"),
        )
        op.create_index("ix_funding_scan_companies_id", "funding_scan_companies", ["id"], unique=False)
        op.create_index("ix_funding_scan_companies_scan", "funding_scan_companies", ["scan_id"], unique=False)
        op.create_index("ix_funding_scan_companies_company", "funding_scan_companies", ["company_id"], unique=False)

    # has_open_positions was dropped in b2c3d4e5f6a7; re-add it.
    fc_cols = {row["name"] for row in sa.inspect(bind).get_columns("funding_companies")} if "funding_companies" in tables else set()
    if "funding_companies" in tables and "has_open_positions" not in fc_cols:
        op.add_column("funding_companies", sa.Column("has_open_positions", sa.Boolean(), nullable=False, server_default="0"))


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "funding_scan_companies" in tables:
        for idx in ("ix_funding_scan_companies_company", "ix_funding_scan_companies_scan", "ix_funding_scan_companies_id"):
            try:
                op.drop_index(idx, table_name="funding_scan_companies")
            except Exception:
                pass
        op.drop_table("funding_scan_companies")
    if "funding_scans" in tables:
        for idx in ("ix_funding_scans_user_scanned", "ix_funding_scans_user_id", "ix_funding_scans_id"):
            try:
                op.drop_index(idx, table_name="funding_scans")
            except Exception:
                pass
        op.drop_table("funding_scans")
    if "has_open_positions" in ({row["name"] for row in sa.inspect(bind).get_columns("funding_companies")} if "funding_companies" in set(sa.inspect(bind).get_table_names()) else set()):
        try:
            op.drop_column("funding_companies", "has_open_positions")
        except Exception:
            pass
