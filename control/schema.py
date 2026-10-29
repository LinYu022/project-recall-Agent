"""Kept separate from source tables; imported by the common migration metadata."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from database.schema import metadata

tasks = sa.Table(
    "control_tasks",
    metadata,
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
revisions = sa.Table(
    "control_revisions",
    metadata,
    sa.Column("revision_id", sa.Text, primary_key=True),
    sa.Column("task_id", sa.Text, sa.ForeignKey("control_tasks.task_id"), nullable=False),
    sa.Column("predecessor_id", sa.Text, sa.ForeignKey("control_revisions.revision_id")),
    sa.Column("binding_task_id", sa.Text, unique=True, nullable=False),
    sa.Column("body", JSONB, nullable=False),
    sa.Column("body_sha256", sa.Text, nullable=False),
    sa.Column("frozen_time", JSONB, nullable=False),
)
authorizations = sa.Table(
    "control_authorizations",
    metadata,
    sa.Column("grant_id", sa.Text, primary_key=True),
    sa.Column("task_id", sa.Text, sa.ForeignKey("control_tasks.task_id"), nullable=False),
    sa.Column("issued_by", sa.Text, nullable=False),
    sa.Column("basis", sa.Text, nullable=False),
    sa.Column("body", JSONB, nullable=False),
    sa.Column("body_sha256", sa.Text, nullable=False),
    sa.Column("revoked_at", sa.DateTime(timezone=True)),
    sa.Column("expires_at", sa.DateTime(timezone=True)),
)
questions = sa.Table(
    "control_questions",
    metadata,
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
executions = sa.Table(
    "control_executions",
    metadata,
    sa.Column("execution_id", sa.Text, primary_key=True),
    sa.Column("task_id", sa.Text, sa.ForeignKey("control_tasks.task_id"), nullable=False),
    sa.Column("revision_id", sa.Text, nullable=False),
    sa.Column("grant_id", sa.Text, nullable=False),
    sa.Column("epoch", sa.Integer, nullable=False),
    sa.Column("permit_sha256", sa.Text, nullable=False),
    sa.Column("state", sa.Text, nullable=False),
    sa.Column("private_report", JSONB),
    sa.CheckConstraint(
        "state IN ('running','completed','failed','interrupted')", name="ck_control_execution_state"
    ),
)
events = sa.Table(
    "control_events",
    metadata,
    sa.Column("task_id", sa.Text, sa.ForeignKey("control_tasks.task_id"), primary_key=True),
    sa.Column("version", sa.Integer, primary_key=True),
    sa.Column("action", sa.Text, nullable=False),
    sa.Column("actor", sa.Text, nullable=False),
    sa.Column("payload", JSONB, nullable=False),
    sa.Column("at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
)
