"""PostgreSQL transaction checks with synthetic source data."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from database.schema import (
    record_versions,
    snapshot_imports,
    snapshot_rows,
    source_snapshots,
    task_specs,
)
from ingestion.importer import first_observed_at, import_snapshot
from ingestion.records import prepare_import
from ingestion.snapshots import SnapshotStore
from tests.conftest import ROOT
from tests.source_fixtures import capture_rows, complaint

pytestmark = pytest.mark.postgres


def test_migration_adds_source_tables_without_erasing_existing_control_rows(database):
    with database.begin() as connection:
        connection.execute(
            task_specs.insert().values(task_id="SYNTHETIC-EXISTING", schema_version="test", spec={})
        )
        config = Config(str(ROOT / "alembic.ini"))
        config.attributes["connection"] = connection
        command.downgrade(config, "0001_control_tables")
        assert not sa.inspect(connection).has_table("record_versions")
        command.upgrade(config, "head")
        assert sa.inspect(connection).has_table("record_versions")
        assert connection.scalar(sa.select(task_specs.c.task_id)) == "SYNTHETIC-EXISTING"


def counts(database):
    with database.connect() as connection:
        return {
            t.name: connection.scalar(sa.select(sa.func.count()).select_from(t))
            for t in (source_snapshots, record_versions, snapshot_imports, snapshot_rows)
        }


def test_reimport_is_idempotent_but_separate_fetch_keeps_observation(database, tmp_path):
    first = capture_rows(tmp_path, [complaint()])
    second = capture_rows(tmp_path, [complaint()])
    assert import_snapshot(database, tmp_path, first.snapshot_id)["new_versions"] == 1
    assert import_snapshot(database, tmp_path, first.snapshot_id)["status"] == "already_imported"
    assert import_snapshot(database, tmp_path, second.snapshot_id)["new_versions"] == 0
    assert counts(database) == {
        "source_snapshots": 2,
        "record_versions": 1,
        "snapshot_imports": 2,
        "snapshot_rows": 2,
    }


def test_changed_content_keeps_old_version_and_old_snapshot_binding(database, tmp_path):
    first = capture_rows(tmp_path, [complaint(summary="SYNTHETIC ORIGINAL")])
    second = capture_rows(tmp_path, [complaint(summary="SYNTHETIC REVISION")])
    for snapshot in (first, second):
        assert import_snapshot(database, tmp_path, snapshot.snapshot_id)["new_versions"] == 1
    old_id = prepare_import(tmp_path, first.snapshot_id).rows[0].version["version_id"]
    with database.connect() as connection:
        assert (
            connection.scalar(
                sa.select(snapshot_rows.c.version_id).where(
                    snapshot_rows.c.snapshot_id == first.snapshot_id
                )
            )
            == old_id
        )
        assert (
            connection.scalar(
                sa.select(record_versions.c.payload).where(record_versions.c.version_id == old_id)
            )["summary"]
            == "SYNTHETIC ORIGINAL"
        )
    assert counts(database)["record_versions"] == 2


def test_first_observation_uses_receipt_time_even_if_imported_out_of_order(database, tmp_path):
    first = capture_rows(tmp_path, [complaint()])
    second = capture_rows(tmp_path, [complaint()])
    # Explicit controlled earlier observation, not a claim about public history.
    first = first.model_copy(
        update={
            "request_started_at": first.request_started_at - timedelta(days=1),
            "completed_at": first.completed_at - timedelta(days=1),
            "fetched_at": first.fetched_at - timedelta(days=1),
        }
    )
    (tmp_path / "snapshots" / f"{first.snapshot_id}.json").write_text(
        first.model_dump_json(), encoding="utf-8"
    )
    version_id = prepare_import(tmp_path, second.snapshot_id).rows[0].version["version_id"]
    import_snapshot(database, tmp_path, second.snapshot_id)
    with database.connect() as connection:
        assert first_observed_at(connection, version_id) == second.fetched_at
    import_snapshot(database, tmp_path, first.snapshot_id)
    with database.connect() as connection:
        assert first_observed_at(connection, version_id) == first.fetched_at


def test_row_failures_are_persisted_with_reconciled_counts(database, tmp_path):
    snapshot = capture_rows(tmp_path, [complaint(), complaint() | {"odiNumber": None}])
    report = import_snapshot(database, tmp_path, snapshot.snapshot_id)
    assert report["accepted"] == report["rejected"] == 1
    assert counts(database)["snapshot_rows"] == 2
    with database.connect() as connection:
        row = (
            connection.execute(sa.select(snapshot_rows).where(snapshot_rows.c.ordinal == 1))
            .mappings()
            .one()
        )
        assert row["version_id"] is None and row["error_code"] == "odi_number_invalid"


def test_duplicate_input_rows_preserve_positions_without_duplicate_versions(database, tmp_path):
    snapshot = capture_rows(tmp_path, [complaint(), complaint()])
    report = import_snapshot(database, tmp_path, snapshot.snapshot_id)
    assert report["accepted"] == 2 and report["distinct_versions"] == report["new_versions"] == 1
    assert counts(database)["snapshot_rows"] == 2


def test_concurrent_reimport_has_one_commit_and_one_idempotent_result(database, tmp_path):
    snapshot = capture_rows(tmp_path, [complaint()])
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda _: import_snapshot(database, tmp_path, snapshot.snapshot_id), range(2))
        )
    assert sorted(r["status"] for r in results) == ["already_imported", "imported"]
    assert counts(database)["record_versions"] == counts(database)["snapshot_imports"] == 1


def test_unchanged_mapping_version_cannot_hide_changed_normalization(
    database, tmp_path, monkeypatch
):
    snapshot = capture_rows(tmp_path, [complaint()])
    import_snapshot(database, tmp_path, snapshot.snapshot_id)
    import ingestion.records as records

    original = records.map_row

    def changed(snapshot, row):
        key, value = original(snapshot, row)
        return key, value | {"unexpected_mapping_change": True}

    monkeypatch.setattr(records, "map_row", changed)
    with pytest.raises(ValueError, match="mapping_version"):
        import_snapshot(database, tmp_path, snapshot.snapshot_id)
    assert counts(database)["record_versions"] == 1


def test_late_database_failure_rolls_back_the_whole_import(database, tmp_path):
    snapshot = capture_rows(tmp_path, [complaint(), complaint(identifier=12345679)])
    with database.begin() as connection:
        connection.execute(
            sa.text("ALTER TABLE snapshot_rows ADD CONSTRAINT injected_failure CHECK (ordinal = 0)")
        )
    with pytest.raises(sa.exc.IntegrityError):
        import_snapshot(database, tmp_path, snapshot.snapshot_id)
    assert all(value == 0 for value in counts(database).values())


def test_existing_snapshot_manifest_cannot_be_rewritten(database, tmp_path):
    snapshot = capture_rows(tmp_path, [complaint()])
    import_snapshot(database, tmp_path, snapshot.snapshot_id)
    path = tmp_path / "snapshots" / f"{snapshot.snapshot_id}.json"
    data = json.loads(path.read_text())
    data["http_headers"] = {"etag": "changed"}
    path.write_text(json.dumps(data), encoding="utf-8")
    SnapshotStore(tmp_path).verify(snapshot.snapshot_id)
    with pytest.raises(ValueError, match="immutable manifest"):
        import_snapshot(database, tmp_path, snapshot.snapshot_id)
