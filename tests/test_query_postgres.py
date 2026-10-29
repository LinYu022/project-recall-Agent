"""Real PostgreSQL plus HTTP contract checks over explicitly synthetic complaints."""

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from app.query_api import QuerySettings, create_query_app
from database.schema import entity_projections, record_versions
from entities.store import project_versions
from tests.source_fixtures import capture_rows, complaint
from tests.test_entity_postgres import prepare
from tests.test_query_api import count_body, temporal

pytestmark = pytest.mark.postgres


def client_app(database, root, identifier):
    return create_query_app(
        QuerySettings(snapshot_root=root, allowed_snapshot_ids=[identifier]),
        engine=database,
        development_mode=True,
    )


def test_count_api_matches_raw_odi_union_and_does_not_mutate_tables(database, tmp_path):
    a = complaint(12345678) | {"components": "POWER TRAIN,SERVICE BRAKES"}
    a["products"] *= 2
    rows = [a, complaint(12345679) | {"components": "POWER TRAIN"}]
    snapshot, _, _ = prepare(database, tmp_path, rows)
    with TestClient(client_app(database, tmp_path, snapshot.snapshot_id)) as client:
        body = count_body(snapshot.snapshot_id, components=["POWER TRAIN", "SERVICE BRAKES"])
        response = client.post("/queries/complaints/count", json=body)
        assert response.status_code == 200
        result = response.json()
        assert result["status"] == "counted" and result["distinct_complaints"] == 2
        assert result["matching_component_rows"] == 3
        assert result["inputs"][0]["snapshot_id"] == snapshot.snapshot_id
        assert client.post("/queries/complaints/count", json=body).json() == result
    with database.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(record_versions)) == 2
        assert connection.scalar(sa.select(sa.func.count()).select_from(entity_projections)) == 2


def test_zero_partial_and_unsupported_are_distinct_api_results(database, tmp_path):
    snapshot, _, _ = prepare(database, tmp_path, [complaint() | {"components": "SERVICE BRAKES"}])
    with TestClient(client_app(database, tmp_path, snapshot.snapshot_id)) as client:
        zero = client.post(
            "/queries/complaints/count",
            json=count_body(snapshot.snapshot_id, components=["POWER TRAIN"]),
        ).json()
        partial = client.post(
            "/queries/complaints/count",
            json=count_body(
                snapshot.snapshot_id,
                production_interval={"start": "2015-01-01", "end": "2017-12-31"},
            ),
        ).json()
        unsupported = client.post(
            "/queries/complaints/count",
            json=count_body(
                snapshot.snapshot_id,
                temporal=temporal([snapshot.snapshot_id], cutoff="2000-01-01T00:00:00Z"),
            ),
        ).json()
    assert zero["status"] == "counted" and zero["distinct_complaints"] == 0
    assert partial["status"] == "partial" and partial["unknown_production_date_versions"] == 1
    assert unsupported["status"] == "unsupported" and unsupported["distinct_complaints"] is None


def test_missing_projection_is_conflict_not_count_zero(database, tmp_path):
    snapshot = capture_rows(tmp_path, [complaint() | {"components": "SERVICE BRAKES"}])
    with TestClient(client_app(database, tmp_path, snapshot.snapshot_id)) as client:
        response = client.post("/queries/complaints/count", json=count_body(snapshot.snapshot_id))
    assert response.status_code == 409 and response.json() == {"status": "projection_not_ready"}


def test_rules_fingerprint_drift_is_conflict(database, tmp_path):
    snapshot, identifiers, rules = prepare(database, tmp_path, [complaint()])
    changed = rules.model_copy(update={"component_labels": rules.component_labels + ["SYNTHETIC"]})
    app = client_app(database, tmp_path, snapshot.snapshot_id)
    app.state.query_services.count_rules = changed
    with TestClient(app) as client:
        response = client.post("/queries/complaints/count", json=count_body(snapshot.snapshot_id))
    assert response.status_code == 409
    assert project_versions(database, identifiers, rules)["new_projections"] == 0


def test_count_uses_read_only_repeatable_read_transaction(database, tmp_path):
    snapshot, _, _ = prepare(database, tmp_path, [complaint() | {"components": "SERVICE BRAKES"}])
    observed = []

    def verify_transaction(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().startswith("SELECT") and "entity_projections" in statement:
            # Use the DBAPI cursor directly so this inspection does not trigger this event again.
            with connection.connection.cursor() as inspector:
                inspector.execute("SHOW transaction_read_only")
                read_only = inspector.fetchone()[0]
                inspector.execute("SHOW transaction_isolation")
                isolation = inspector.fetchone()[0]
                observed.append((read_only, isolation))

    sa.event.listen(database, "before_cursor_execute", verify_transaction)
    try:
        with TestClient(client_app(database, tmp_path, snapshot.snapshot_id)) as client:
            assert (
                client.post(
                    "/queries/complaints/count", json=count_body(snapshot.snapshot_id)
                ).status_code
                == 200
            )
    finally:
        sa.event.remove(database, "before_cursor_execute", verify_transaction)
    assert observed == [("on", "repeatable read")]


def test_database_failure_does_not_expose_connection_or_source_data(database, tmp_path):
    snapshot, _, _ = prepare(database, tmp_path, [complaint() | {"components": "SERVICE BRAKES"}])

    def fail(connection, cursor, statement, parameters, context, executemany):
        if "entity_projections" in statement:
            raise sa.exc.OperationalError(statement, {}, RuntimeError("PRIVATE_DB_PASSWORD"))

    sa.event.listen(database, "before_cursor_execute", fail)
    try:
        with TestClient(client_app(database, tmp_path, snapshot.snapshot_id)) as client:
            response = client.post(
                "/queries/complaints/count", json=count_body(snapshot.snapshot_id)
            )
    finally:
        sa.event.remove(database, "before_cursor_execute", fail)
    assert response.status_code == 503 and "PRIVATE_DB_PASSWORD" not in response.text
