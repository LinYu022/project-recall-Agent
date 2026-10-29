"""Release identities, atomic defaults and task/report retention pins."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0004_dataset_releases"
down_revision = "0003_entity_projections"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "dataset_releases",
        sa.Column("release_id", sa.Text, primary_key=True),
        sa.Column("dataset_build_id", sa.Text, nullable=False),
        sa.Column("bundle_key", sa.Text, nullable=False, unique=True),
        sa.Column("manifest_sha256", sa.Text, nullable=False, unique=True),
        sa.Column("manifest", JSONB, nullable=False),
        sa.Column("state", sa.Text, nullable=False, server_default="available"),
        sa.Column("retain_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "state IN ('available', 'retiring', 'collected')", name="ck_release_state"
        ),
        sa.CheckConstraint("jsonb_typeof(manifest) = 'object'", name="ck_release_manifest_object"),
    )
    op.create_table(
        "release_records",
        sa.Column(
            "release_id", sa.Text, sa.ForeignKey("dataset_releases.release_id"), primary_key=True
        ),
        sa.Column(
            "version_id", sa.Text, sa.ForeignKey("record_versions.version_id"), primary_key=True
        ),
        sa.Column("record_sha256", sa.Text, nullable=False),
    )
    op.create_table(
        "release_defaults",
        sa.Column("slot", sa.Text, primary_key=True),
        sa.Column("release_id", sa.Text, sa.ForeignKey("dataset_releases.release_id")),
        sa.CheckConstraint("slot = 'default'", name="ck_release_default_slot"),
    )
    op.create_table(
        "release_tasks",
        sa.Column("task_id", sa.Text, primary_key=True),
        sa.Column(
            "release_id", sa.Text, sa.ForeignKey("dataset_releases.release_id"), nullable=False
        ),
        sa.Column("spec", JSONB, nullable=False),
        sa.Column("spec_sha256", sa.Text, nullable=False),
        sa.Column("state", sa.Text, nullable=False, server_default="active"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "state IN ('active', 'finished', 'cancelled')", name="ck_release_task_state"
        ),
        sa.CheckConstraint("jsonb_typeof(spec) = 'object'", name="ck_release_task_spec"),
    )
    op.create_table(
        "release_report_refs",
        sa.Column("reference_id", sa.Text, primary_key=True),
        sa.Column(
            "release_id", sa.Text, sa.ForeignKey("dataset_releases.release_id"), nullable=False
        ),
        sa.Column("task_id", sa.Text, sa.ForeignKey("release_tasks.task_id"), nullable=False),
        sa.Column("artifact_sha256", sa.Text, nullable=False),
        sa.Column("keep_until", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "release_events",
        sa.Column("event_id", UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "release_id", sa.Text, sa.ForeignKey("dataset_releases.release_id"), nullable=False
        ),
        sa.Column("action", sa.Text, nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("jsonb_typeof(payload) = 'object'", name="ck_release_event_object"),
    )


def downgrade():
    for name in (
        "release_events",
        "release_report_refs",
        "release_tasks",
        "release_defaults",
        "release_records",
        "dataset_releases",
    ):
        op.drop_table(name)
