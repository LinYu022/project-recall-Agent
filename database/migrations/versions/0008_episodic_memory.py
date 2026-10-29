"""Frozen P1-14 episodic snapshots, navigation index and adoption records."""

from alembic import op

revision = "0008_episodic_memory"
down_revision = "0007_semantic_memory"
branch_labels = None
depends_on = None


def upgrade():

    op.execute("""
CREATE TABLE memory_episodes (
	episode_id TEXT NOT NULL,
	owner_id TEXT NOT NULL,
	project_id TEXT NOT NULL,
	task_id TEXT NOT NULL,
	task_version INTEGER NOT NULL,
	body JSONB NOT NULL,
	body_sha256 TEXT NOT NULL,
	revision INTEGER NOT NULL,
	enabled BOOLEAN NOT NULL,
	captured_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (episode_id),
	CONSTRAINT uq_episode_snapshot UNIQUE (owner_id, project_id, task_id, task_version),
	CONSTRAINT ck_episode_versions CHECK (revision >= 1 AND task_version >= 1),
	FOREIGN KEY(task_id) REFERENCES control_tasks (task_id)
)
""")

    op.execute("""
CREATE TABLE memory_episode_events (
	event_id TEXT NOT NULL,
	episode_id TEXT NOT NULL,
	ordinal INTEGER NOT NULL,
	body JSONB NOT NULL,
	body_sha256 TEXT NOT NULL,
	PRIMARY KEY (event_id),
	CONSTRAINT uq_episode_event_ordinal UNIQUE (episode_id, ordinal),
	CONSTRAINT ck_episode_event_ordinal CHECK (ordinal >= 0),
	FOREIGN KEY(episode_id) REFERENCES memory_episodes (episode_id)
)
""")

    op.execute("""
CREATE TABLE memory_episode_adoptions (
	execution_id TEXT NOT NULL,
	episode_id TEXT NOT NULL,
	episode_revision INTEGER NOT NULL,
	task_id TEXT NOT NULL,
	owner_id TEXT NOT NULL,
	reference JSONB NOT NULL,
	PRIMARY KEY (execution_id, episode_id),
	FOREIGN KEY(execution_id) REFERENCES control_executions (execution_id),
	FOREIGN KEY(episode_id) REFERENCES memory_episodes (episode_id),
	FOREIGN KEY(task_id) REFERENCES control_tasks (task_id)
)
""")


def downgrade():

    op.drop_table("memory_episode_adoptions")

    op.drop_table("memory_episode_events")

    op.drop_table("memory_episodes")
