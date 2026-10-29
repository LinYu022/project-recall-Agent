"""Frozen P1-13 reviewed memory, extraction queue and derived indexes."""

from alembic import op

revision = "0007_semantic_memory"
down_revision = "0006_durable_dispatch"
branch_labels = None
depends_on = None


def upgrade():

    op.execute("""
CREATE TABLE memory_cache (
	cache_key TEXT NOT NULL,
	owner_id TEXT NOT NULL,
	generation INTEGER NOT NULL,
	"references" JSONB NOT NULL,
	PRIMARY KEY (cache_key)
)
""")

    op.execute("""
CREATE TABLE memory_events (
	event_id TEXT NOT NULL,
	owner_id TEXT NOT NULL,
	memory_id TEXT,
	action TEXT NOT NULL,
	snapshot JSONB NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (event_id)
)
""")

    op.execute("""
CREATE TABLE memory_projects (
	owner_id TEXT NOT NULL,
	project_id TEXT NOT NULL,
	name TEXT NOT NULL,
	enabled BOOLEAN NOT NULL,
	PRIMARY KEY (owner_id, project_id)
)
""")

    op.execute("""
CREATE TABLE memory_sources (
	source_id TEXT NOT NULL,
	owner_id TEXT NOT NULL,
	project_id TEXT,
	origin TEXT NOT NULL,
	text TEXT NOT NULL,
	content_sha256 TEXT NOT NULL,
	PRIMARY KEY (source_id),
	CONSTRAINT ck_memory_origin CHECK (origin IN ('user_statement','untrusted_text'))
)
""")

    op.execute("""
CREATE TABLE memory_states (
	owner_id TEXT NOT NULL,
	generation INTEGER NOT NULL,
	enabled BOOLEAN NOT NULL,
	PRIMARY KEY (owner_id),
	CONSTRAINT ck_memory_generation CHECK (generation >= 1)
)
""")

    op.execute("""
CREATE TABLE memory_candidates (
	candidate_id TEXT NOT NULL,
	owner_id TEXT NOT NULL,
	source_id TEXT NOT NULL,
	proposal JSONB NOT NULL,
	proposal_sha256 TEXT NOT NULL,
	fingerprint TEXT NOT NULL,
	state TEXT NOT NULL,
	revision INTEGER NOT NULL,
	supersedes TEXT,
	supersedes_revision INTEGER,
	review JSONB,
	PRIMARY KEY (candidate_id),
	CONSTRAINT uq_memory_candidate UNIQUE (owner_id, fingerprint),
	CONSTRAINT ck_memory_candidate_state CHECK (state IN ('pending','approved','rejected')),
	FOREIGN KEY(source_id) REFERENCES memory_sources (source_id)
)
""")

    op.execute("""
CREATE TABLE memory_extractions (
	job_id TEXT NOT NULL,
	owner_id TEXT NOT NULL,
	source_id TEXT NOT NULL,
	state TEXT NOT NULL,
	candidate_ids JSONB NOT NULL,
	error_code TEXT,
	PRIMARY KEY (job_id),
	CONSTRAINT uq_memory_extraction_source UNIQUE (owner_id, source_id),
	CONSTRAINT ck_memory_extraction_state CHECK (state IN ('queued','completed','failed')),
	FOREIGN KEY(source_id) REFERENCES memory_sources (source_id)
)
""")

    op.execute("""
CREATE TABLE memory_items (
	memory_id TEXT NOT NULL,
	owner_id TEXT NOT NULL,
	candidate_id TEXT NOT NULL,
	body JSONB NOT NULL,
	body_sha256 TEXT NOT NULL,
	state TEXT NOT NULL,
	revision INTEGER NOT NULL,
	validation JSONB NOT NULL,
	PRIMARY KEY (memory_id),
	CONSTRAINT ck_memory_item_state CHECK (state IN ('active','disabled','deleted','expired','superseded')),
	FOREIGN KEY(candidate_id) REFERENCES memory_candidates (candidate_id)
)
""")

    op.execute("""
CREATE TABLE memory_active_index (
	memory_id TEXT NOT NULL,
	owner_id TEXT NOT NULL,
	revision INTEGER NOT NULL,
	search_text TEXT NOT NULL,
	PRIMARY KEY (memory_id),
	FOREIGN KEY(memory_id) REFERENCES memory_items (memory_id)
)
""")

    op.execute("""
CREATE TABLE memory_adoptions (
	execution_id TEXT NOT NULL,
	memory_id TEXT NOT NULL,
	revision INTEGER NOT NULL,
	task_id TEXT NOT NULL,
	owner_id TEXT NOT NULL,
	reference JSONB NOT NULL,
	PRIMARY KEY (execution_id, memory_id, revision),
	FOREIGN KEY(execution_id) REFERENCES control_executions (execution_id),
	FOREIGN KEY(memory_id) REFERENCES memory_items (memory_id),
	FOREIGN KEY(task_id) REFERENCES control_tasks (task_id)
)
""")


def downgrade():

    op.drop_table("memory_adoptions")

    op.drop_table("memory_active_index")

    op.drop_table("memory_items")

    op.drop_table("memory_extractions")

    op.drop_table("memory_candidates")

    op.drop_table("memory_states")

    op.drop_table("memory_sources")

    op.drop_table("memory_projects")

    op.drop_table("memory_events")

    op.drop_table("memory_cache")
