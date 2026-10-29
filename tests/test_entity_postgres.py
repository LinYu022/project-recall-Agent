"""Real SQL constraints and joins, using synthetic source rows."""

import pytest
import sqlalchemy as sa

from database.schema import entity_components, entity_projections, entity_vehicles, record_versions
from entities.counts import ComplaintCountRequest, count_complaints
from entities.normalize import load_rules
from entities.store import project_versions
from ingestion.importer import import_snapshot
from ingestion.records import prepare_import
from tests.source_fixtures import capture_rows, complaint

pytestmark = pytest.mark.postgres


def request(snapshot, **updates):
    return ComplaintCountRequest.model_validate(
        {
            "temporal": {
                "snapshot_ids": [snapshot.snapshot_id],
                "time": {
                    "mode": "snapshot_analysis",
                    "event_interval": None,
                    "knowledge_cutoff": None,
                    "date_filter_basis": "Controlled counting check",
                },
            },
            "make": "FORD",
            "model": "F-150",
            "model_year": 2016,
            "components": ["POWER TRAIN", "SERVICE BRAKES"],
        }
        | updates
    )


def prepare(database, tmp_path, rows):
    snapshot = capture_rows(tmp_path, rows)
    import_snapshot(database, tmp_path, snapshot.snapshot_id)
    identifiers = [
        row.version["version_id"]
        for row in prepare_import(tmp_path, snapshot.snapshot_id).rows
        if row.version
    ]
    rules = load_rules()
    project_versions(database, identifiers, rules)
    return snapshot, identifiers, rules


def test_many_products_and_components_do_not_multiply_complaint_count(database, tmp_path):
    row = complaint() | {"components": "POWER TRAIN,SERVICE BRAKES"}
    row["products"] *= 2
    snapshot, identifiers, rules = prepare(database, tmp_path, [row])
    result = count_complaints(database, tmp_path, request(snapshot), rules)
    assert result["distinct_complaints"] == result["matched_versions"] == 1
    assert result["matching_component_rows"] == 2
    with database.connect() as connection:
        naive = connection.scalar(
            sa.select(sa.func.count()).select_from(
                entity_vehicles.join(
                    entity_components,
                    sa.and_(
                        entity_vehicles.c.version_id == entity_components.c.version_id,
                        entity_vehicles.c.rules_version == entity_components.c.rules_version,
                    ),
                )
            )
        )
    assert naive == 4


def test_projection_is_idempotent_and_never_rewrites_source(database, tmp_path):
    snapshot, identifiers, rules = prepare(
        database, tmp_path, [complaint() | {"components": "SERVICE BRAKES"}]
    )
    assert project_versions(database, identifiers, rules)["new_projections"] == 0
    with database.connect() as connection:
        assert (
            connection.scalar(sa.select(record_versions.c.payload))["components"]
            == "SERVICE BRAKES"
        )
        assert connection.scalar(sa.select(sa.func.count()).select_from(entity_projections)) == 1


def test_unknown_production_date_cannot_satisfy_requested_range(database, tmp_path):
    snapshot, _, rules = prepare(
        database, tmp_path, [complaint() | {"components": "SERVICE BRAKES"}]
    )
    result = count_complaints(
        database,
        tmp_path,
        request(snapshot, production_interval={"start": "2015-01-01", "end": "2017-12-31"}),
        rules,
    )
    assert result["status"] == "partial"
    assert result["distinct_complaints"] == 0 and result["unknown_production_date_versions"] == 1


def test_query_refuses_fuzzy_names_and_unprojected_records(database, tmp_path):
    snapshot = capture_rows(tmp_path, [complaint() | {"components": "SERVICE BRAKES"}])
    import_snapshot(database, tmp_path, snapshot.snapshot_id)
    rules = load_rules()
    with pytest.raises(ValueError, match="projection"):
        count_complaints(database, tmp_path, request(snapshot), rules)
    with pytest.raises(ValueError, match="exact mapped"):
        count_complaints(database, tmp_path, request(snapshot, model="F150"), rules)


def test_component_filter_counts_a_union_not_a_sum(database, tmp_path):
    rows = [
        complaint(12345678) | {"components": "POWER TRAIN,SERVICE BRAKES"},
        complaint(12345679) | {"components": "POWER TRAIN"},
        complaint(12345680) | {"components": "UNKNOWN OR OTHER"},
    ]
    snapshot, _, rules = prepare(database, tmp_path, rows)
    result = count_complaints(database, tmp_path, request(snapshot), rules)
    assert result["distinct_complaints"] == 2 and result["matching_component_rows"] == 3
    assert (
        count_complaints(database, tmp_path, request(snapshot, components=[]), rules)[
            "distinct_complaints"
        ]
        == 3
    )


def test_changed_rules_need_a_new_rules_version(database, tmp_path):
    snapshot, identifiers, rules = prepare(
        database, tmp_path, [complaint() | {"components": "SERVICE BRAKES"}]
    )
    changed = rules.model_copy(
        update={"component_labels": rules.component_labels + ["SYNTHETIC EXTRA"]}
    )
    with pytest.raises(ValueError, match="rules_version"):
        project_versions(database, identifiers, changed)
    with pytest.raises(ValueError, match="exact entity projection"):
        count_complaints(database, tmp_path, request(snapshot), changed)
    revised = changed.model_copy(update={"rules_version": "synthetic-rules/0.2"})
    assert project_versions(database, identifiers, revised)["new_projections"] == 1
    with database.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(entity_projections)) == 2


def test_query_text_is_not_executed_as_sql(database, tmp_path):
    snapshot, _, rules = prepare(
        database, tmp_path, [complaint() | {"components": "SERVICE BRAKES"}]
    )
    with pytest.raises(ValueError):
        count_complaints(
            database,
            tmp_path,
            request(snapshot, model="F-150'; DROP TABLE record_versions; --"),
            rules,
        )
    with database.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(record_versions)) == 1


def test_projection_transaction_rolls_back_on_late_storage_failure(database, tmp_path):
    snapshot = capture_rows(tmp_path, [complaint() | {"components": "POWER TRAIN,SERVICE BRAKES"}])
    import_snapshot(database, tmp_path, snapshot.snapshot_id)
    identifiers = [
        row.version["version_id"]
        for row in prepare_import(tmp_path, snapshot.snapshot_id).rows
        if row.version
    ]
    with database.begin() as connection:
        connection.execute(
            sa.text(
                "ALTER TABLE entity_components ADD CONSTRAINT injected_component_failure CHECK(position=0)"
            )
        )
    with pytest.raises(sa.exc.IntegrityError):
        project_versions(database, identifiers, load_rules())
    with database.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(entity_projections)) == 0
        assert connection.scalar(sa.select(sa.func.count()).select_from(entity_vehicles)) == 0
