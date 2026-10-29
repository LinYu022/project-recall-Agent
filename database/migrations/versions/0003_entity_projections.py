"""Versioned entity projections retain source values and one-to-many positions."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0003_entity_projections"
down_revision = "0002_source_records"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "entity_projections",
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
    op.create_table(
        "entity_vehicles",
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
    op.create_table(
        "entity_components",
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


def downgrade():
    op.drop_table("entity_components")
    op.drop_table("entity_vehicles")
    op.drop_table("entity_projections")
