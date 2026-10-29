"""Primary memory records, derived index/cache and immutable audit snapshots."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from database.schema import metadata

states = sa.Table(
    "memory_states",
    metadata,
    sa.Column("owner_id", sa.Text, primary_key=True),
    sa.Column("generation", sa.Integer, nullable=False),
    sa.Column("enabled", sa.Boolean, nullable=False),
    sa.CheckConstraint("generation >= 1", name="ck_memory_generation"),
)
projects = sa.Table(
    "memory_projects",
    metadata,
    sa.Column("owner_id", sa.Text, primary_key=True),
    sa.Column("project_id", sa.Text, primary_key=True),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("enabled", sa.Boolean, nullable=False),
)
sources = sa.Table(
    "memory_sources",
    metadata,
    sa.Column("source_id", sa.Text, primary_key=True),
    sa.Column("owner_id", sa.Text, nullable=False),
    sa.Column("project_id", sa.Text),
    sa.Column("origin", sa.Text, nullable=False),
    sa.Column("text", sa.Text, nullable=False),
    sa.Column("content_sha256", sa.Text, nullable=False),
    sa.CheckConstraint("origin IN ('user_statement','untrusted_text')", name="ck_memory_origin"),
)
candidates = sa.Table(
    "memory_candidates",
    metadata,
    sa.Column("candidate_id", sa.Text, primary_key=True),
    sa.Column("owner_id", sa.Text, nullable=False),
    sa.Column("source_id", sa.Text, sa.ForeignKey("memory_sources.source_id"), nullable=False),
    sa.Column("proposal", JSONB, nullable=False),
    sa.Column("proposal_sha256", sa.Text, nullable=False),
    sa.Column("fingerprint", sa.Text, nullable=False),
    sa.Column("state", sa.Text, nullable=False),
    sa.Column("revision", sa.Integer, nullable=False),
    sa.Column("supersedes", sa.Text),
    sa.Column("supersedes_revision", sa.Integer),
    sa.Column("review", JSONB),
    sa.UniqueConstraint("owner_id", "fingerprint", name="uq_memory_candidate"),
    sa.CheckConstraint(
        "state IN ('pending','approved','rejected')", name="ck_memory_candidate_state"
    ),
)
items = sa.Table(
    "memory_items",
    metadata,
    sa.Column("memory_id", sa.Text, primary_key=True),
    sa.Column("owner_id", sa.Text, nullable=False),
    sa.Column(
        "candidate_id", sa.Text, sa.ForeignKey("memory_candidates.candidate_id"), nullable=False
    ),
    sa.Column("body", JSONB, nullable=False),
    sa.Column("body_sha256", sa.Text, nullable=False),
    sa.Column("state", sa.Text, nullable=False),
    sa.Column("revision", sa.Integer, nullable=False),
    sa.Column("validation", JSONB, nullable=False),
    sa.CheckConstraint(
        "state IN ('active','disabled','deleted','expired','superseded')",
        name="ck_memory_item_state",
    ),
)
index = sa.Table(
    "memory_active_index",
    metadata,
    sa.Column("memory_id", sa.Text, sa.ForeignKey("memory_items.memory_id"), primary_key=True),
    sa.Column("owner_id", sa.Text, nullable=False),
    sa.Column("revision", sa.Integer, nullable=False),
    sa.Column("search_text", sa.Text, nullable=False),
)
cache = sa.Table(
    "memory_cache",
    metadata,
    sa.Column("cache_key", sa.Text, primary_key=True),
    sa.Column("owner_id", sa.Text, nullable=False),
    sa.Column("generation", sa.Integer, nullable=False),
    sa.Column("references", JSONB, nullable=False),
)
extractions = sa.Table(
    "memory_extractions",
    metadata,
    sa.Column("job_id", sa.Text, primary_key=True),
    sa.Column("owner_id", sa.Text, nullable=False),
    sa.Column("source_id", sa.Text, sa.ForeignKey("memory_sources.source_id"), nullable=False),
    sa.Column("state", sa.Text, nullable=False),
    sa.Column("candidate_ids", JSONB, nullable=False),
    sa.Column("error_code", sa.Text),
    sa.UniqueConstraint("owner_id", "source_id", name="uq_memory_extraction_source"),
    sa.CheckConstraint(
        "state IN ('queued','completed','failed')", name="ck_memory_extraction_state"
    ),
)
events = sa.Table(
    "memory_events",
    metadata,
    sa.Column("event_id", sa.Text, primary_key=True),
    sa.Column("owner_id", sa.Text, nullable=False),
    sa.Column("memory_id", sa.Text),
    sa.Column("action", sa.Text, nullable=False),
    sa.Column("snapshot", JSONB, nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    ),
)
adoptions = sa.Table(
    "memory_adoptions",
    metadata,
    sa.Column(
        "execution_id", sa.Text, sa.ForeignKey("control_executions.execution_id"), primary_key=True
    ),
    sa.Column("memory_id", sa.Text, sa.ForeignKey("memory_items.memory_id"), primary_key=True),
    sa.Column("revision", sa.Integer, primary_key=True),
    sa.Column("task_id", sa.Text, sa.ForeignKey("control_tasks.task_id"), nullable=False),
    sa.Column("owner_id", sa.Text, nullable=False),
    sa.Column("reference", JSONB, nullable=False),
)
