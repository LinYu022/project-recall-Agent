"""Real database bindings with explicitly controlled observation dates."""

import pytest
import sqlalchemy as sa

from database.schema import snapshot_rows
from ingestion.importer import import_snapshot
from ingestion.temporal import select_versions
from tests.source_fixtures import complaint
from tests.test_temporal import observed, request

pytestmark = pytest.mark.postgres


def test_historical_selection_uses_old_database_version_after_revision_import(database, tmp_path):
    old = observed(tmp_path, [complaint(summary="SYNTHETIC BEFORE")], "2020-02-01T00:00:00+00:00")
    new = observed(tmp_path, [complaint(summary="SYNTHETIC AFTER")], "2020-04-01T00:00:00+00:00")
    for snapshot in [new, old]:
        import_snapshot(database, tmp_path, snapshot.snapshot_id)
    selected = select_versions(tmp_path, request([old, new]))
    with database.connect() as connection:
        before = connection.scalar(
            sa.select(snapshot_rows.c.version_id).where(
                snapshot_rows.c.snapshot_id == old.snapshot_id
            )
        )
        after = connection.scalar(
            sa.select(snapshot_rows.c.version_id).where(
                snapshot_rows.c.snapshot_id == new.snapshot_id
            )
        )
    assert selected["selected"][0]["version_id"] == before
    assert selected["excluded"][0]["version_id"] == after


def test_fixed_snapshot_stays_bound_when_late_complaint_is_imported(database, tmp_path):
    old = observed(tmp_path, [complaint()], "2020-02-01T00:00:00+00:00")
    late = observed(
        tmp_path,
        [complaint(12345679, event="01/01/2010") | {"dateComplaintFiled": "04/01/2020"}],
        "2020-04-01T00:00:00+00:00",
    )
    import_snapshot(database, tmp_path, old.snapshot_id)
    pinned = request([old], "snapshot_analysis", None)
    before = select_versions(tmp_path, pinned)
    import_snapshot(database, tmp_path, late.snapshot_id)
    assert select_versions(tmp_path, pinned) == before
    historical = select_versions(tmp_path, request([old, late]))
    assert historical["selected_count"] == historical["excluded_count"] == 1
