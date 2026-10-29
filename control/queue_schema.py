"""Durable dispatch and a root-task resource pool shared by all descendants."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from control.schema import executions
from database.schema import metadata

pools = sa.Table(
    "dispatch_pools",
    metadata,
    sa.Column("root_task_id", sa.Text, sa.ForeignKey("control_tasks.task_id"), primary_key=True),
    sa.Column("owner_id", sa.Text, nullable=False),
    sa.Column("limits", JSONB, nullable=False),
    sa.Column("limits_sha256", sa.Text, nullable=False),
    sa.Column("used", JSONB, nullable=False),
    sa.Column("held", JSONB, nullable=False),
)
jobs = sa.Table(
    "dispatch_jobs",
    metadata,
    sa.Column("job_id", sa.Text, primary_key=True),
    sa.Column("owner_id", sa.Text, nullable=False),
    sa.Column("task_id", sa.Text, sa.ForeignKey("control_tasks.task_id"), nullable=False),
    sa.Column(
        "root_task_id", sa.Text, sa.ForeignKey("dispatch_pools.root_task_id"), nullable=False
    ),
    sa.Column("idempotency_key", sa.Text, nullable=False),
    sa.Column("pinned", JSONB, nullable=False),
    sa.Column("pinned_sha256", sa.Text, nullable=False),
    sa.Column("state", sa.Text, nullable=False),
    sa.Column("attempt", sa.Integer, nullable=False),
    sa.Column("execution_id", sa.Text),
    sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("deadline", sa.DateTime(timezone=True), nullable=False),
    sa.Column("max_attempts", sa.Integer, nullable=False),
    sa.Column("lease_seconds", sa.Integer, nullable=False),
    sa.Column("last_error", sa.Text),
    sa.UniqueConstraint("owner_id", "task_id", "idempotency_key", name="uq_dispatch_submission"),
    sa.CheckConstraint(
        "state IN ('queued','running','waiting_input','completed','failed','cancelled')",
        name="ck_dispatch_state",
    ),
    sa.CheckConstraint(
        "attempt >= 0 AND max_attempts BETWEEN 1 AND 5 AND lease_seconds BETWEEN 2 AND 300",
        name="ck_dispatch_bounds",
    ),
)
calls = sa.Table(
    "dispatch_calls",
    metadata,
    sa.Column("call_id", sa.Text, primary_key=True),
    sa.Column("job_id", sa.Text, sa.ForeignKey("dispatch_jobs.job_id"), nullable=False),
    sa.Column(
        "execution_id", sa.Text, sa.ForeignKey("control_executions.execution_id"), nullable=False
    ),
    sa.Column("ordinal", sa.Integer, nullable=False),
    sa.Column("attempt", sa.Integer, nullable=False),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("fingerprint", sa.Text, nullable=False),
    sa.Column("state", sa.Text, nullable=False),
    sa.Column("reserved", JSONB, nullable=False),
    sa.Column("charged", JSONB),
    sa.Column("input_artifact", JSONB, nullable=False),
    sa.Column("output_artifact", JSONB),
    sa.Column("usage", JSONB),
    sa.Column("pending_reconciliation", sa.Boolean, nullable=False),
    sa.Column("late", sa.Boolean, nullable=False),
    sa.Column("error_code", sa.Text),
    sa.UniqueConstraint("job_id", "ordinal", "attempt", name="uq_dispatch_call_attempt"),
    sa.CheckConstraint(
        "state IN ('reserved','completed','error','unknown')", name="ck_dispatch_call_state"
    ),
    sa.CheckConstraint(
        "ordinal >= 0 AND attempt >= 1 AND kind IN ('model','tool')", name="ck_dispatch_call_bounds"
    ),
)

executions.append_column(sa.Column("job_id", sa.Text, sa.ForeignKey("dispatch_jobs.job_id")))
executions.append_column(sa.Column("lease_until", sa.DateTime(timezone=True)))
executions.append_column(sa.Column("claim_version", sa.Integer))
