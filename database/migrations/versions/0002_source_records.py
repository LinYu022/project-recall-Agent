"""Append-only source receipts, mapped versions, imports and row lineage."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0002_source_records"
down_revision = "0001_control_tables"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "source_snapshots",
        sa.Column("snapshot_id", sa.Text, primary_key=True),
        sa.Column("source_id", sa.Text, nullable=False),
        sa.Column("raw_sha256", sa.Text, nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("manifest", JSONB, nullable=False),
        sa.CheckConstraint("jsonb_typeof(manifest) = 'object'", name="ck_source_manifest_object"),
    )
    op.create_table(
        "record_versions",
        sa.Column("version_id", sa.Text, primary_key=True),
        sa.Column("source_kind", sa.Text, nullable=False),
        sa.Column("record_key", sa.Text, nullable=False),
        sa.Column("content_sha256", sa.Text, nullable=False),
        sa.Column("mapping_version", sa.Text, nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("normalized", JSONB, nullable=False),
        sa.UniqueConstraint(
            "source_kind",
            "record_key",
            "content_sha256",
            "mapping_version",
            name="uq_record_content_mapping",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(payload) = 'object' AND jsonb_typeof(normalized) = 'object'",
            name="ck_record_objects",
        ),
    )
    op.create_table(
        "snapshot_imports",
        sa.Column(
            "snapshot_id",
            sa.Text,
            sa.ForeignKey("source_snapshots.snapshot_id", name="fk_import_snapshot"),
            primary_key=True,
        ),
        sa.Column("mapping_version", sa.Text, primary_key=True),
        sa.Column("input_units", sa.Integer, nullable=False),
        sa.Column("accepted", sa.Integer, nullable=False),
        sa.Column("rejected", sa.Integer, nullable=False),
        sa.Column("report", JSONB, nullable=False),
        sa.CheckConstraint(
            "input_units >= 0 AND accepted >= 0 AND rejected >= 0 AND input_units = accepted + rejected",
            name="ck_import_counts",
        ),
        sa.CheckConstraint("jsonb_typeof(report) = 'object'", name="ck_import_report_object"),
    )
    op.create_table(
        "snapshot_rows",
        sa.Column("snapshot_id", sa.Text, primary_key=True),
        sa.Column("mapping_version", sa.Text, primary_key=True),
        sa.Column("ordinal", sa.Integer, primary_key=True),
        sa.Column("raw_pointer", sa.Text, nullable=False),
        sa.Column(
            "version_id",
            sa.Text,
            sa.ForeignKey("record_versions.version_id", name="fk_row_version"),
        ),
        sa.Column("error_code", sa.Text),
        sa.ForeignKeyConstraint(
            ["snapshot_id", "mapping_version"],
            ["snapshot_imports.snapshot_id", "snapshot_imports.mapping_version"],
            name="fk_row_import",
        ),
        sa.CheckConstraint("ordinal >= 0", name="ck_row_ordinal"),
        sa.CheckConstraint(
            "(version_id IS NOT NULL AND error_code IS NULL) OR (version_id IS NULL AND error_code IS NOT NULL)",
            name="ck_row_outcome",
        ),
    )


def downgrade():
    op.drop_table("snapshot_rows")
    op.drop_table("snapshot_imports")
    op.drop_table("record_versions")
    op.drop_table("source_snapshots")
