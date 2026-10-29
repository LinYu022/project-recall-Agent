"""Initial control-data schema; scheduling and lifecycle rules come later."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

metadata = sa.MetaData()

task_specs = sa.Table(
    "task_specs",
    metadata,
    sa.Column("task_id", sa.Text, primary_key=True),
    sa.Column("schema_version", sa.Text, nullable=False),
    sa.Column("spec", JSONB, nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    ),
    sa.CheckConstraint("jsonb_typeof(spec) = 'object'", name="ck_task_spec_object"),
)

execution_runs = sa.Table(
    "execution_runs",
    metadata,
    sa.Column("run_id", UUID(as_uuid=False), primary_key=True),
    sa.Column(
        "task_id", sa.Text, sa.ForeignKey("task_specs.task_id", name="fk_run_task"), nullable=False
    ),
    sa.Column("release_id", sa.Text),
    sa.Column("grant_id", sa.Text),
    sa.Column("revision_id", sa.Text),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("manifest", JSONB, nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    ),
    sa.CheckConstraint(
        "status IN ('succeeded', 'failed', 'denied')", name="ck_run_terminal_status"
    ),
    sa.CheckConstraint("jsonb_typeof(manifest) = 'object'", name="ck_run_manifest_object"),
)

runtime_events = sa.Table(
    "runtime_events",
    metadata,
    sa.Column("event_id", UUID(as_uuid=False), primary_key=True),
    sa.Column(
        "run_id",
        UUID(as_uuid=False),
        sa.ForeignKey("execution_runs.run_id", name="fk_event_run"),
        nullable=False,
    ),
    sa.Column("call_id", UUID(as_uuid=False), nullable=False),
    sa.Column("execution_generation", sa.Integer, nullable=False),
    sa.Column("sequence", sa.Integer, nullable=False),
    sa.Column("payload", JSONB, nullable=False),
    sa.UniqueConstraint("run_id", "sequence", name="uq_event_run_sequence"),
    sa.CheckConstraint("sequence > 0 AND execution_generation > 0", name="ck_event_positive_order"),
    sa.CheckConstraint("jsonb_typeof(payload) = 'object'", name="ck_event_payload_object"),
)

source_snapshots = sa.Table(
    "source_snapshots",
    metadata,
    sa.Column("snapshot_id", sa.Text, primary_key=True),
    sa.Column("source_id", sa.Text, nullable=False),
    sa.Column("raw_sha256", sa.Text, nullable=False),
    sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("manifest", JSONB, nullable=False),
    sa.CheckConstraint("jsonb_typeof(manifest) = 'object'", name="ck_source_manifest_object"),
)

record_versions = sa.Table(
    "record_versions",
    metadata,
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

snapshot_imports = sa.Table(
    "snapshot_imports",
    metadata,
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

snapshot_rows = sa.Table(
    "snapshot_rows",
    metadata,
    sa.Column("snapshot_id", sa.Text, primary_key=True),
    sa.Column("mapping_version", sa.Text, primary_key=True),
    sa.Column("ordinal", sa.Integer, primary_key=True),
    sa.Column("raw_pointer", sa.Text, nullable=False),
    sa.Column(
        "version_id", sa.Text, sa.ForeignKey("record_versions.version_id", name="fk_row_version")
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

entity_projections = sa.Table(
    "entity_projections",
    metadata,
    sa.Column(
        "version_id",
        sa.Text,
        sa.ForeignKey("record_versions.version_id", name="fk_projection_record"),
        primary_key=True,
    ),
    sa.Column("rules_version", sa.Text, primary_key=True),
    sa.Column("rules_sha256", sa.Text, nullable=False),
    sa.Column("projection", JSONB, nullable=False),
    sa.CheckConstraint("jsonb_typeof(projection) = 'object'", name="ck_projection_object"),
)

entity_vehicles = sa.Table(
    "entity_vehicles",
    metadata,
    sa.Column("version_id", sa.Text, primary_key=True),
    sa.Column("rules_version", sa.Text, primary_key=True),
    sa.Column("position", sa.Integer, primary_key=True),
    sa.Column("make", sa.Text),
    sa.Column("model", sa.Text),
    sa.Column("model_year", sa.Integer),
    sa.Column("production_date", sa.Date),
    sa.ForeignKeyConstraint(
        ["version_id", "rules_version"],
        ["entity_projections.version_id", "entity_projections.rules_version"],
        name="fk_vehicle_projection",
    ),
    sa.CheckConstraint("position >= 0", name="ck_vehicle_position"),
    sa.CheckConstraint(
        "model_year IS NULL OR model_year BETWEEN 1886 AND 2100", name="ck_vehicle_year"
    ),
)

entity_components = sa.Table(
    "entity_components",
    metadata,
    sa.Column("version_id", sa.Text, primary_key=True),
    sa.Column("rules_version", sa.Text, primary_key=True),
    sa.Column("position", sa.Integer, primary_key=True),
    sa.Column("canonical", sa.Text),
    sa.Column("raw_label", sa.Text),
    sa.ForeignKeyConstraint(
        ["version_id", "rules_version"],
        ["entity_projections.version_id", "entity_projections.rules_version"],
        name="fk_component_projection",
    ),
    sa.CheckConstraint("position >= 0", name="ck_component_position"),
)

dataset_releases = sa.Table(
    "dataset_releases",
    metadata,
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
    sa.CheckConstraint("state IN ('available', 'retiring', 'collected')", name="ck_release_state"),
    sa.CheckConstraint("jsonb_typeof(manifest) = 'object'", name="ck_release_manifest_object"),
)

release_records = sa.Table(
    "release_records",
    metadata,
    sa.Column(
        "release_id", sa.Text, sa.ForeignKey("dataset_releases.release_id"), primary_key=True
    ),
    sa.Column("version_id", sa.Text, sa.ForeignKey("record_versions.version_id"), primary_key=True),
    sa.Column("record_sha256", sa.Text, nullable=False),
)

release_defaults = sa.Table(
    "release_defaults",
    metadata,
    sa.Column("slot", sa.Text, primary_key=True),
    sa.Column("release_id", sa.Text, sa.ForeignKey("dataset_releases.release_id")),
    sa.CheckConstraint("slot = 'default'", name="ck_release_default_slot"),
)

release_tasks = sa.Table(
    "release_tasks",
    metadata,
    sa.Column("task_id", sa.Text, primary_key=True),
    sa.Column("release_id", sa.Text, sa.ForeignKey("dataset_releases.release_id"), nullable=False),
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

release_report_refs = sa.Table(
    "release_report_refs",
    metadata,
    sa.Column("reference_id", sa.Text, primary_key=True),
    sa.Column("release_id", sa.Text, sa.ForeignKey("dataset_releases.release_id"), nullable=False),
    sa.Column("task_id", sa.Text, sa.ForeignKey("release_tasks.task_id"), nullable=False),
    sa.Column("artifact_sha256", sa.Text, nullable=False),
    sa.Column("keep_until", sa.DateTime(timezone=True)),
)

release_events = sa.Table(
    "release_events",
    metadata,
    sa.Column("event_id", UUID(as_uuid=False), primary_key=True),
    sa.Column("release_id", sa.Text, sa.ForeignKey("dataset_releases.release_id"), nullable=False),
    sa.Column("action", sa.Text, nullable=False),
    sa.Column("payload", JSONB, nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    ),
    sa.CheckConstraint("jsonb_typeof(payload) = 'object'", name="ck_release_event_object"),
)
