"""Frozen P1-11 dispatch, shared resource reservations and lease fences."""

import sqlalchemy as sa
from alembic import op

revision = "0006_durable_dispatch"
down_revision = "0005_task_control"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
CREATE TABLE dispatch_pools (
	root_task_id TEXT NOT NULL,
	owner_id TEXT NOT NULL,
	limits JSONB NOT NULL,
	limits_sha256 TEXT NOT NULL,
	used JSONB NOT NULL,
	held JSONB NOT NULL,
	PRIMARY KEY (root_task_id),
	FOREIGN KEY(root_task_id) REFERENCES control_tasks (task_id)
)
""")
    op.execute("""
CREATE TABLE dispatch_jobs (
	job_id TEXT NOT NULL,
	owner_id TEXT NOT NULL,
	task_id TEXT NOT NULL,
	root_task_id TEXT NOT NULL,
	idempotency_key TEXT NOT NULL,
	pinned JSONB NOT NULL,
	pinned_sha256 TEXT NOT NULL,
	state TEXT NOT NULL,
	attempt INTEGER NOT NULL,
	execution_id TEXT,
	available_at TIMESTAMP WITH TIME ZONE NOT NULL,
	deadline TIMESTAMP WITH TIME ZONE NOT NULL,
	max_attempts INTEGER NOT NULL,
	lease_seconds INTEGER NOT NULL,
	last_error TEXT,
	PRIMARY KEY (job_id),
	CONSTRAINT uq_dispatch_submission UNIQUE (owner_id, task_id, idempotency_key),
	CONSTRAINT ck_dispatch_state CHECK (state IN ('queued','running','waiting_input','completed','failed','cancelled')),
	CONSTRAINT ck_dispatch_bounds CHECK (attempt >= 0 AND max_attempts BETWEEN 1 AND 5 AND lease_seconds BETWEEN 2 AND 300),
	FOREIGN KEY(task_id) REFERENCES control_tasks (task_id),
	FOREIGN KEY(root_task_id) REFERENCES dispatch_pools (root_task_id)
)
""")
    op.execute("""
CREATE TABLE dispatch_calls (
	call_id TEXT NOT NULL,
	job_id TEXT NOT NULL,
	execution_id TEXT NOT NULL,
	ordinal INTEGER NOT NULL,
	attempt INTEGER NOT NULL,
	kind TEXT NOT NULL,
	fingerprint TEXT NOT NULL,
	state TEXT NOT NULL,
	reserved JSONB NOT NULL,
	charged JSONB,
	input_artifact JSONB NOT NULL,
	output_artifact JSONB,
	usage JSONB,
	pending_reconciliation BOOLEAN NOT NULL,
	late BOOLEAN NOT NULL,
	error_code TEXT,
	PRIMARY KEY (call_id),
	CONSTRAINT uq_dispatch_call_attempt UNIQUE (job_id, ordinal, attempt),
	CONSTRAINT ck_dispatch_call_state CHECK (state IN ('reserved','completed','error','unknown')),
	CONSTRAINT ck_dispatch_call_bounds CHECK (ordinal >= 0 AND attempt >= 1 AND kind IN ('model','tool')),
	FOREIGN KEY(job_id) REFERENCES dispatch_jobs (job_id),
	FOREIGN KEY(execution_id) REFERENCES control_executions (execution_id)
)
""")
    op.add_column("control_executions", sa.Column("job_id", sa.Text))
    op.create_foreign_key(
        "fk_execution_dispatch_job", "control_executions", "dispatch_jobs", ["job_id"], ["job_id"]
    )
    op.add_column("control_executions", sa.Column("lease_until", sa.DateTime(timezone=True)))
    op.add_column("control_executions", sa.Column("claim_version", sa.Integer))


def downgrade():
    op.drop_column("control_executions", "claim_version")
    op.drop_column("control_executions", "lease_until")
    op.drop_constraint("fk_execution_dispatch_job", "control_executions", type_="foreignkey")
    op.drop_column("control_executions", "job_id")
    op.drop_table("dispatch_calls")
    op.drop_table("dispatch_jobs")
    op.drop_table("dispatch_pools")
