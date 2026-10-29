"""Immutable episodic snapshots, derived event rows, and auditable adoption events."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from database.schema import metadata

episodes = sa.Table(
    "memory_episodes",
    metadata,
    sa.Column("episode_id", sa.Text, primary_key=True),
    sa.Column("owner_id", sa.Text, nullable=False),
    sa.Column("project_id", sa.Text, nullable=False),
    sa.Column("task_id", sa.Text, sa.ForeignKey("control_tasks.task_id"), nullable=False),
    sa.Column("task_version", sa.Integer, nullable=False),
    sa.Column("body", JSONB, nullable=False),
    sa.Column("body_sha256", sa.Text, nullable=False),
    sa.Column("revision", sa.Integer, nullable=False),
    sa.Column("enabled", sa.Boolean, nullable=False),
    sa.Column(
        "captured_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    ),
    sa.UniqueConstraint(
        "owner_id", "project_id", "task_id", "task_version", name="uq_episode_snapshot"
    ),
    sa.CheckConstraint("revision >= 1 AND task_version >= 1", name="ck_episode_versions"),
)
events = sa.Table(
    "memory_episode_events",
    metadata,
    sa.Column("event_id", sa.Text, primary_key=True),
    sa.Column("episode_id", sa.Text, sa.ForeignKey("memory_episodes.episode_id"), nullable=False),
    sa.Column("ordinal", sa.Integer, nullable=False),
    sa.Column("body", JSONB, nullable=False),
    sa.Column("body_sha256", sa.Text, nullable=False),
    sa.UniqueConstraint("episode_id", "ordinal", name="uq_episode_event_ordinal"),
    sa.CheckConstraint("ordinal >= 0", name="ck_episode_event_ordinal"),
)
adoptions = sa.Table(
    "memory_episode_adoptions",
    metadata,
    sa.Column(
        "execution_id", sa.Text, sa.ForeignKey("control_executions.execution_id"), primary_key=True
    ),
    sa.Column("episode_id", sa.Text, sa.ForeignKey("memory_episodes.episode_id"), primary_key=True),
    sa.Column("episode_revision", sa.Integer, nullable=False),
    sa.Column("task_id", sa.Text, sa.ForeignKey("control_tasks.task_id"), nullable=False),
    sa.Column("owner_id", sa.Text, nullable=False),
    sa.Column("reference", JSONB, nullable=False),
)
