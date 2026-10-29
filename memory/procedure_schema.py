"""Version lifecycle, immutable validation proofs and task adoption of procedure memory."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from database.schema import metadata

versions = sa.Table(
    "memory_procedure_versions",
    metadata,
    sa.Column("version_id", sa.Text, primary_key=True),
    sa.Column("owner_id", sa.Text, nullable=False),
    sa.Column("project_id", sa.Text, nullable=False),
    sa.Column("skill_key", sa.Text, nullable=False),
    sa.Column("version_number", sa.Integer, nullable=False),
    sa.Column("revision", sa.Integer, nullable=False),
    sa.Column("state", sa.Text, nullable=False),
    sa.Column("definition", JSONB, nullable=False),
    sa.Column("definition_sha256", sa.Text, nullable=False),
    sa.Column("origins", JSONB, nullable=False),
    sa.Column("validation_id", sa.Text),
    sa.UniqueConstraint(
        "owner_id", "project_id", "skill_key", "version_number", name="uq_procedure_version_number"
    ),
    sa.CheckConstraint(
        "state IN ('candidate','validated','enabled','deprecated','revoked')",
        name="ck_procedure_state",
    ),
    sa.CheckConstraint("version_number >= 1 AND revision >= 1", name="ck_procedure_versions"),
)
heads = sa.Table(
    "memory_procedure_heads",
    metadata,
    sa.Column("owner_id", sa.Text, primary_key=True),
    sa.Column("project_id", sa.Text, primary_key=True),
    sa.Column("skill_key", sa.Text, primary_key=True),
    sa.Column("revision", sa.Integer, nullable=False),
    sa.Column("version_id", sa.Text, sa.ForeignKey("memory_procedure_versions.version_id")),
    sa.CheckConstraint("revision >= 1", name="ck_procedure_head_revision"),
)
validations = sa.Table(
    "memory_procedure_validations",
    metadata,
    sa.Column("validation_id", sa.Text, primary_key=True),
    sa.Column(
        "version_id", sa.Text, sa.ForeignKey("memory_procedure_versions.version_id"), nullable=False
    ),
    sa.Column("owner_id", sa.Text, nullable=False),
    sa.Column("idempotency_key", sa.Text, nullable=False),
    sa.Column("input_sha256", sa.Text, nullable=False),
    sa.Column("state", sa.Text, nullable=False),
    sa.Column("tool_calls", sa.Integer, nullable=False),
    sa.Column("tool_limit", sa.Integer, nullable=False),
    sa.Column("proof", JSONB),
    sa.UniqueConstraint(
        "owner_id", "version_id", "idempotency_key", name="uq_procedure_validation_request"
    ),
    sa.CheckConstraint(
        "state IN ('running','passed','failed')", name="ck_procedure_validation_state"
    ),
    sa.CheckConstraint(
        "tool_calls >= 0 AND tool_limit BETWEEN 1 AND 12 AND tool_calls <= tool_limit",
        name="ck_procedure_validation_budget",
    ),
)
adoptions = sa.Table(
    "memory_procedure_adoptions",
    metadata,
    sa.Column(
        "execution_id", sa.Text, sa.ForeignKey("control_executions.execution_id"), primary_key=True
    ),
    sa.Column(
        "version_id",
        sa.Text,
        sa.ForeignKey("memory_procedure_versions.version_id"),
        primary_key=True,
    ),
    sa.Column("owner_id", sa.Text, nullable=False),
    sa.Column("task_id", sa.Text, sa.ForeignKey("control_tasks.task_id"), nullable=False),
    sa.Column("reference", JSONB, nullable=False),
)
