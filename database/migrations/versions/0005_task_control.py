"""Frozen P1-08 control schema: grants, revisions, lifecycle, questions, executions and audit."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0005_task_control"
down_revision = "0004_dataset_releases"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "control_tasks",
        sa.Column("task_id", sa.Text, primary_key=True),
        sa.Column("owner_id", sa.Text, nullable=False),
        sa.Column("parent_id", sa.Text, sa.ForeignKey("control_tasks.task_id")),
        sa.Column("parent_revision_id", sa.Text),
        sa.Column("parent_grant_id", sa.Text),
        sa.Column("parent_epoch", sa.Integer),
        sa.Column("state", sa.Text, nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("epoch", sa.Integer, nullable=False),
        sa.Column("revision_id", sa.Text),
        sa.Column("grant_id", sa.Text),
        sa.Column("execution_id", sa.Text),
        sa.Column("published_report", JSONB),
        sa.Column("quality", JSONB, nullable=False),
        sa.CheckConstraint(
            "state IN ('DRAFT','WAITING_INPUT','WAITING_APPROVAL','RUNNING','PAUSED','COMPLETED','FAILED','CANCELLED')",
            name="ck_control_state",
        ),
        sa.CheckConstraint("version >= 1 AND epoch >= 1", name="ck_control_versions"),
    )
    op.create_table(
        "control_revisions",
        sa.Column("revision_id", sa.Text, primary_key=True),
        sa.Column("task_id", sa.Text, sa.ForeignKey("control_tasks.task_id"), nullable=False),
        sa.Column("predecessor_id", sa.Text, sa.ForeignKey("control_revisions.revision_id")),
        sa.Column("binding_task_id", sa.Text, unique=True, nullable=False),
        sa.Column("body", JSONB, nullable=False),
        sa.Column("body_sha256", sa.Text, nullable=False),
        sa.Column("frozen_time", JSONB, nullable=False),
    )
    op.create_table(
        "control_authorizations",
        sa.Column("grant_id", sa.Text, primary_key=True),
        sa.Column("task_id", sa.Text, sa.ForeignKey("control_tasks.task_id"), nullable=False),
        sa.Column("issued_by", sa.Text, nullable=False),
        sa.Column("basis", sa.Text, nullable=False),
        sa.Column("body", JSONB, nullable=False),
        sa.Column("body_sha256", sa.Text, nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "control_questions",
        sa.Column("question_id", sa.Text, primary_key=True),
        sa.Column("task_id", sa.Text, sa.ForeignKey("control_tasks.task_id"), nullable=False),
        sa.Column(
            "revision_id", sa.Text, sa.ForeignKey("control_revisions.revision_id"), nullable=False
        ),
        sa.Column("epoch", sa.Integer, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("fields", JSONB, nullable=False),
        sa.Column("answer", JSONB),
        sa.CheckConstraint("kind IN ('input','approval')", name="ck_control_question_kind"),
    )
    op.create_table(
        "control_executions",
        sa.Column("execution_id", sa.Text, primary_key=True),
        sa.Column("task_id", sa.Text, sa.ForeignKey("control_tasks.task_id"), nullable=False),
        sa.Column("revision_id", sa.Text, nullable=False),
        sa.Column("grant_id", sa.Text, nullable=False),
        sa.Column("epoch", sa.Integer, nullable=False),
        sa.Column("permit_sha256", sa.Text, nullable=False),
        sa.Column("state", sa.Text, nullable=False),
        sa.Column("private_report", JSONB),
        sa.CheckConstraint(
            "state IN ('running','completed','failed','interrupted')",
            name="ck_control_execution_state",
        ),
    )
    op.create_table(
        "control_events",
        sa.Column("task_id", sa.Text, sa.ForeignKey("control_tasks.task_id"), primary_key=True),
        sa.Column("version", sa.Integer, primary_key=True),
        sa.Column("action", sa.Text, nullable=False),
        sa.Column("actor", sa.Text, nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade():
    for name in (
        "control_events",
        "control_executions",
        "control_questions",
        "control_authorizations",
        "control_revisions",
        "control_tasks",
    ):
        op.drop_table(name)
