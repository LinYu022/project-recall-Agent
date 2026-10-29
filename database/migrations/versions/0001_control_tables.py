"""Initial task, execution and audit tables; frozen schema revision."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0001_control_tables"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "task_specs",
        sa.Column("task_id", sa.Text, primary_key=True),
        sa.Column("schema_version", sa.Text, nullable=False),
        sa.Column("spec", JSONB, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("jsonb_typeof(spec) = 'object'", name="ck_task_spec_object"),
    )
    op.create_table(
        "execution_runs",
        sa.Column("run_id", UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "task_id",
            sa.Text,
            sa.ForeignKey("task_specs.task_id", name="fk_run_task"),
            nullable=False,
        ),
        sa.Column("release_id", sa.Text),
        sa.Column("grant_id", sa.Text),
        sa.Column("revision_id", sa.Text),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("manifest", JSONB, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "status IN ('succeeded', 'failed', 'denied')", name="ck_run_terminal_status"
        ),
        sa.CheckConstraint("jsonb_typeof(manifest) = 'object'", name="ck_run_manifest_object"),
    )
    op.create_table(
        "runtime_events",
        sa.Column("event_id", UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "run_id",
            UUID(as_uuid=False),
            sa.ForeignKey("execution_runs.run_id", name="fk_event_run"),
            nullable=False,
        ),
        sa.Column("call_id", UUID(as_uuid=False), nullable=False),
        sa.Column("execution_generation", sa.Integer, nullable=False),
        sa.Column("sequence", sa.Integer, nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.UniqueConstraint("run_id", "sequence", name="uq_event_run_sequence"),
        sa.CheckConstraint(
            "sequence > 0 AND execution_generation > 0", name="ck_event_positive_order"
        ),
        sa.CheckConstraint("jsonb_typeof(payload) = 'object'", name="ck_event_payload_object"),
    )


def downgrade():
    op.drop_table("runtime_events")
    op.drop_table("execution_runs")
    op.drop_table("task_specs")
