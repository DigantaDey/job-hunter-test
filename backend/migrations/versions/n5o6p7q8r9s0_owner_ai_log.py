"""owner ai log — exact requests sent, and how Laya is doing.

Two tables implement the v2.3 owner-only observability surface
(``app.services.ai_log``):

* ``ai_call_records`` — one row per AI provider call: the outbound request body
  per attempt (already scrubbed; headers are never captured), the routing
  parameters, the outcome, tokens/cost, and a bounded excerpt of the answer.
  The credit ledger answers "how much"; this answers "what exactly did we send".
* ``laya_decisions`` — one row per local-engine forward pass: task, status
  (``ok`` / ``low_confidence`` / ``timeout`` / ``error`` / ``parked``),
  checkpoint, question count, calibrated confidence against the active floor,
  latency, and the compact per-question verdict.

Both carry a real ``user_id`` FK for the same reason the pool's ``seen`` table
does: ``app.services.erasure`` derives its plan from the schema, so account
deletion reaches these rows without a special case. Both are bounded by
``AI_LOG_RETENTION_DAYS`` on write. Both are read exclusively by the
owner-only ``/api/admin/ai/*`` routes.

Idempotent like its siblings: an existing table is left alone, so the CI job
that runs ``alembic upgrade head`` twice on a database that already has the
tables (the ``create_all`` path used by tests and throwaway environments
creates them from the models) is a no-op, not an error.

Revision ID: n5o6p7q8r9s0
Revises: m4n5o6p7q8r9
Create Date: 2026-09-26
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "n5o6p7q8r9s0"
down_revision: Union[str, None] = "m4n5o6p7q8r9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())

    if "ai_call_records" not in existing:
        op.create_table(
            "ai_call_records",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("workflow", sa.String(length=40), nullable=False, server_default=""),
            sa.Column("provider", sa.String(length=32), nullable=True, server_default=""),
            sa.Column("model", sa.String(length=120), nullable=True, server_default=""),
            sa.Column("base_url", sa.String(length=300), nullable=True, server_default=""),
            sa.Column("status", sa.String(length=16), nullable=False, server_default="ok"),
            sa.Column("reason", sa.String(length=60), nullable=True, server_default=""),
            sa.Column("http_status", sa.Integer(), nullable=True),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("completion_tokens", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("total_tokens", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("estimated_cost_usd", sa.Float(), nullable=False, server_default="0"),
            sa.Column("request", sa.JSON(), nullable=True),
            sa.Column("response", sa.Text(), nullable=True, server_default=""),
            sa.Column("error", sa.Text(), nullable=True, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_ai_call_records_id", "ai_call_records", ["id"], unique=False)
        op.create_index("ix_ai_call_records_user_id", "ai_call_records", ["user_id"], unique=False)
        op.create_index("ix_ai_call_records_status", "ai_call_records", ["status"], unique=False)
        op.create_index("ix_ai_call_created", "ai_call_records", ["created_at"], unique=False)
        op.create_index("ix_ai_call_user_created", "ai_call_records", ["user_id", "created_at"], unique=False)
        op.create_index("ix_ai_call_workflow_created", "ai_call_records", ["workflow", "created_at"], unique=False)

    if "laya_decisions" not in existing:
        op.create_table(
            "laya_decisions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("task", sa.String(length=32), nullable=False, server_default=""),
            sa.Column("status", sa.String(length=24), nullable=False, server_default="ok"),
            sa.Column("model", sa.String(length=48), nullable=True, server_default=""),
            sa.Column("questions", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("confidence", sa.Float(), nullable=True),
            sa.Column("floor", sa.Float(), nullable=True),
            sa.Column("strict", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("answers", sa.JSON(), nullable=True),
            sa.Column("error", sa.Text(), nullable=True, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_laya_decisions_id", "laya_decisions", ["id"], unique=False)
        op.create_index("ix_laya_decisions_user_id", "laya_decisions", ["user_id"], unique=False)
        op.create_index("ix_laya_decisions_status", "laya_decisions", ["status"], unique=False)
        op.create_index("ix_laya_decision_created", "laya_decisions", ["created_at"], unique=False)
        op.create_index("ix_laya_decision_user_created", "laya_decisions", ["user_id", "created_at"], unique=False)
        op.create_index("ix_laya_decision_task_created", "laya_decisions", ["task", "created_at"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    if "laya_decisions" in existing:
        op.drop_table("laya_decisions")
    if "ai_call_records" in existing:
        op.drop_table("ai_call_records")
