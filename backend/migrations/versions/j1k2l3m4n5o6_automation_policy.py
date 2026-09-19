"""Automation policy — versioned user permissions, revision history, daily counters.

``docs/contracts/10-automation-policy.md`` §2. Adds:

* ``automation_policies`` — one row per ``(user_id, scope, scope_key, workflow)``.
  The ``global``/``*`` row is the base; narrower rows (portal / company /
  persona / workflow) override it. ``mode_chosen_at`` is NULL until the user
  makes an explicit choice, which is what separates "inherited plan default"
  from "the user turned this on" for the onboarding gate. ``auto_submit`` is
  never a default on any plan.
* ``automation_policy_revisions`` — append-only. Every change freezes the whole
  row ``document`` plus ``changed_keys`` / actor / reason / request id, so
  "who turned auto-apply on and when" is answerable without reading audit prose.
  ``policy_id`` is deliberately *not* an FK: a revision must outlive its (maybe
  narrow-scoped) row because the submissions that resolved against it do.
* ``automation_submission_counters`` — the atomic per-day backstop behind the
  daily run limit. One row per ``(user_id, period)``; ``record`` counts actions
  that went out, ``rejected`` counts refusals (recorded, never charged toward
  the limit). PostgreSQL gets real ``SELECT … FOR UPDATE`` semantics; on SQLite
  the unique key plus the caller's transaction make the read-increment atomic.

``application_submissions`` grows the three provenance columns that make a
submission reconstructible — ``policy_id``, ``policy_version`` and the
``consent_snapshot`` in force at decision time (contract §5 rule 3).

Revision ID: j1k2l3m4n5o6
Revises: i9j0k1l2m3n4
Create Date: 2026-09-19
"""

from alembic import op
import sqlalchemy as sa

revision: str = "j1k2l3m4n5o6"
down_revision: str = "i9j0k1l2m3n4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- extend the at-most-once ledger with policy provenance --------------- #
    op.add_column("application_submissions", sa.Column("policy_id", sa.Integer(), nullable=True))
    op.add_column("application_submissions", sa.Column("policy_version", sa.Integer(), nullable=True))
    op.add_column("application_submissions", sa.Column("consent_snapshot", sa.JSON(), nullable=True))

    # --- the policy -------------------------------------------- #
    op.create_table(
        "automation_policies",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("scope", sa.String(16), nullable=False, server_default="global"),
        sa.Column("scope_key", sa.String(200), nullable=False, server_default="*"),
        sa.Column("workflow", sa.String(28), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("mode", sa.String(16), nullable=False, server_default="off"),
        sa.Column("mode_chosen_at", sa.DateTime(), nullable=True),
        sa.Column("min_match_score", sa.Float(), nullable=True),
        sa.Column("max_runs_per_day", sa.Integer(), nullable=True),
        sa.Column("max_runs_per_month", sa.Integer(), nullable=True),
        sa.Column("require_review_before_submit", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("require_resume_approval", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("allowed_portals", sa.JSON(), nullable=True),
        sa.Column("blocked_portals", sa.JSON(), nullable=True),
        sa.Column("allowed_companies", sa.JSON(), nullable=True),
        sa.Column("blocked_companies", sa.JSON(), nullable=True),
        sa.Column("blocked_keywords", sa.JSON(), nullable=True),
        sa.Column("allowed_locations", sa.JSON(), nullable=True),
        sa.Column("require_remote", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("require_onsite", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("compensation_floor", sa.JSON(), nullable=True),
        sa.Column("sensitive_field_policy", sa.String(20), nullable=False,
                  server_default="ask_every_time"),
        sa.Column("eeo_policy", sa.String(20), nullable=False, server_default="prefer_decline"),
        sa.Column("credential_policy", sa.String(20), nullable=False,
                  server_default="create_on_demand"),
        sa.Column("schedule", sa.JSON(), nullable=True),
        sa.Column("notify", sa.JSON(), nullable=True),
        sa.Column("consents_required", sa.JSON(), nullable=True),
        sa.Column("consent_snapshot", sa.JSON(), nullable=True),
        sa.Column("disclosure_version", sa.String(24), nullable=False, server_default=""),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("updated_by", sa.String(20), nullable=False, server_default="system_api"),
        sa.Column("effective_from", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("user_id", "scope", "scope_key", "workflow",
                            name="uq_automation_policy_identity"),
    )
    op.create_index("ix_automation_policies_id", "automation_policies", ["id"])
    op.create_index("ix_automation_policies_user_id", "automation_policies", ["user_id"])
    op.create_index("ix_automation_policy_owner_workflow", "automation_policies",
                    ["user_id", "workflow", "enabled"])
    op.create_index("ix_automation_policy_resolution", "automation_policies",
                    ["user_id", "workflow", "scope", "scope_key"])

    # --- the append-only revision history ------------------------ #
    op.create_table(
        "automation_policy_revisions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("policy_id", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("document", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("changed_keys", sa.JSON(), nullable=True),
        sa.Column("actor_type", sa.String(20), nullable=False, server_default="system_api"),
        sa.Column("actor_id", sa.Integer(), nullable=True),
        sa.Column("reason", sa.String(200), nullable=True, server_default=""),
        sa.Column("request_id", sa.String(64), nullable=True, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("user_id", "policy_id", "version",
                            name="uq_automation_revision_version"),
    )
    op.create_index("ix_automation_policy_revisions_id", "automation_policy_revisions", ["id"])
    op.create_index("ix_automation_policy_revisions_user_id", "automation_policy_revisions", ["user_id"])
    op.create_index("ix_automation_policy_revisions_policy_id", "automation_policy_revisions", ["policy_id"])
    op.create_index("ix_automation_revision_owner_policy", "automation_policy_revisions",
                    ["user_id", "policy_id", "created_at"])

    # --- the atomic daily window -------------------------------- #
    op.create_table(
        "automation_submission_counters",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("workflow", sa.String(28), nullable=False, server_default="application_submit"),
        sa.Column("period", sa.String(10), nullable=False),
        sa.Column("record", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rejected", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("user_id", "period", name="uq_automation_counter_user_period"),
    )
    op.create_index("ix_automation_submission_counters_id", "automation_submission_counters", ["id"])
    op.create_index("ix_automation_submission_counters_user_id", "automation_submission_counters", ["user_id"])
    op.create_index("ix_automation_counter_owner_period", "automation_submission_counters",
                    ["user_id", "period"])


def downgrade() -> None:
    for idx in (
        "ix_automation_counter_owner_period",
        "ix_automation_submission_counters_user_id",
        "ix_automation_submission_counters_id",
    ):
        try:
            op.drop_index(idx, table_name="automation_submission_counters")
        except Exception:
            pass
    try:
        op.drop_table("automation_submission_counters")
    except Exception:
        pass

    for idx in (
        "ix_automation_revision_owner_policy",
        "ix_automation_policy_revisions_policy_id",
        "ix_automation_policy_revisions_user_id",
        "ix_automation_policy_revisions_id",
    ):
        try:
            op.drop_index(idx, table_name="automation_policy_revisions")
        except Exception:
            pass
    try:
        op.drop_table("automation_policy_revisions")
    except Exception:
        pass

    for idx in (
        "ix_automation_policy_resolution",
        "ix_automation_policy_owner_workflow",
        "ix_automation_policies_user_id",
        "ix_automation_policies_id",
    ):
        try:
            op.drop_index(idx, table_name="automation_policies")
        except Exception:
            pass
    try:
        op.drop_table("automation_policies")
    except Exception:
        pass

    op.drop_column("application_submissions", "consent_snapshot")
    op.drop_column("application_submissions", "policy_version")
    op.drop_column("application_submissions", "policy_id")
