"""Frozen P1-15 procedure versions, heads, validation and adoption records."""

from alembic import op

revision = "0009_procedural_memory"
down_revision = "0008_episodic_memory"
branch_labels = None
depends_on = None


def upgrade():

    op.execute("""
CREATE TABLE memory_procedure_versions (
	version_id TEXT NOT NULL,
	owner_id TEXT NOT NULL,
	project_id TEXT NOT NULL,
	skill_key TEXT NOT NULL,
	version_number INTEGER NOT NULL,
	revision INTEGER NOT NULL,
	state TEXT NOT NULL,
	definition JSONB NOT NULL,
	definition_sha256 TEXT NOT NULL,
	origins JSONB NOT NULL,
	validation_id TEXT,
	PRIMARY KEY (version_id),
	CONSTRAINT uq_procedure_version_number UNIQUE (owner_id, project_id, skill_key, version_number),
	CONSTRAINT ck_procedure_state CHECK (state IN ('candidate','validated','enabled','deprecated','revoked')),
	CONSTRAINT ck_procedure_versions CHECK (version_number >= 1 AND revision >= 1)
)
""")

    op.execute("""
CREATE TABLE memory_procedure_heads (
	owner_id TEXT NOT NULL,
	project_id TEXT NOT NULL,
	skill_key TEXT NOT NULL,
	revision INTEGER NOT NULL,
	version_id TEXT,
	PRIMARY KEY (owner_id, project_id, skill_key),
	CONSTRAINT ck_procedure_head_revision CHECK (revision >= 1),
	FOREIGN KEY(version_id) REFERENCES memory_procedure_versions (version_id)
)
""")

    op.execute("""
CREATE TABLE memory_procedure_validations (
	validation_id TEXT NOT NULL,
	version_id TEXT NOT NULL,
	owner_id TEXT NOT NULL,
	idempotency_key TEXT NOT NULL,
	input_sha256 TEXT NOT NULL,
	state TEXT NOT NULL,
	tool_calls INTEGER NOT NULL,
	tool_limit INTEGER NOT NULL,
	proof JSONB,
	PRIMARY KEY (validation_id),
	CONSTRAINT uq_procedure_validation_request UNIQUE (owner_id, version_id, idempotency_key),
	CONSTRAINT ck_procedure_validation_state CHECK (state IN ('running','passed','failed')),
	CONSTRAINT ck_procedure_validation_budget CHECK (tool_calls >= 0 AND tool_limit BETWEEN 1 AND 12 AND tool_calls <= tool_limit),
	FOREIGN KEY(version_id) REFERENCES memory_procedure_versions (version_id)
)
""")

    op.execute("""
CREATE TABLE memory_procedure_adoptions (
	execution_id TEXT NOT NULL,
	version_id TEXT NOT NULL,
	owner_id TEXT NOT NULL,
	task_id TEXT NOT NULL,
	reference JSONB NOT NULL,
	PRIMARY KEY (execution_id, version_id),
	FOREIGN KEY(execution_id) REFERENCES control_executions (execution_id),
	FOREIGN KEY(version_id) REFERENCES memory_procedure_versions (version_id),
	FOREIGN KEY(task_id) REFERENCES control_tasks (task_id)
)
""")


def downgrade():

    op.drop_table("memory_procedure_adoptions")

    op.drop_table("memory_procedure_validations")

    op.drop_table("memory_procedure_heads")

    op.drop_table("memory_procedure_versions")
