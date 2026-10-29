"""Real PostgreSQL transactions with synthetic source bytes/models and real local Qdrant."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from app.release_api import create_release_app
from database.schema import (
    dataset_releases,
    entity_vehicles,
    record_versions,
    release_defaults,
    release_records,
    release_tasks,
    snapshot_rows,
)
from releases.catalog import Catalog, ReleaseConflict, ReleaseUnavailable
from releases.contracts import BindTask, PublishRequest, ReleaseQuery, ReportReference
from releases.service import PublishedService
from runtime.artifacts import ArtifactIntegrityError
from tests.release_fixtures import release_factory as release_factory
from tests.test_retrieval import FakeEncoder, FakeReranker

pytestmark = pytest.mark.postgres


def publish(catalog, bundle, expected=None, **kwargs):
    return catalog.publish(
        PublishRequest(
            **{k: bundle["sealed"][k] for k in ("release_id", "manifest_sha256")},
            expected_default=expected,
            retain_until=datetime.now(UTC) + timedelta(days=1),
            **kwargs,
        )
    )


def bind_request(bundle, name, explicit=False):
    return BindTask(
        task_id=name,
        release_id=bundle["sealed"]["release_id"] if explicit else None,
        allowed_source_ids=bundle["manifest"]["allowed_source_ids"],
        scope={"make": "TOYOTA", "model": "PRIUS", "model_year": 2010},
    )


def service(catalog, tmp_path):
    return PublishedService(
        catalog,
        tmp_path / "traces",
        model_factory=lambda role: FakeEncoder() if role == "encoder" else FakeReranker(),
    )


def current(database):
    with database.connect() as connection:
        return connection.scalar(sa.select(release_defaults.c.release_id))


def test_running_task_reads_old_release_after_new_publish_and_rollback(
    database, release_factory, tmp_path
):
    first, second = release_factory("A"), release_factory("B")
    catalog = Catalog(database, first["store"])
    publish(catalog, first)
    catalog.bind(bind_request(first, "TASK-OLD"))
    runner = service(catalog, tmp_path)
    before = runner.query(ReleaseQuery(task_id="TASK-OLD", query="brake", method="bm25"))
    publish(catalog, second, first["sealed"]["release_id"])
    old = runner.query(ReleaseQuery(task_id="TASK-OLD", query="brake", method="bm25"))
    assert old["release_id"] == before["release_id"] == first["sealed"]["release_id"]
    assert old["matched_hits"][0]["text"] == before["matched_hits"][0]["text"]
    assert (
        catalog.bind(bind_request(first, "TASK-OLD"))["release_id"] == first["sealed"]["release_id"]
    )
    catalog.bind(bind_request(second, "TASK-NEW"))
    assert (
        runner.query(ReleaseQuery(task_id="TASK-NEW", query="brake", method="hybrid"))["release_id"]
        == second["sealed"]["release_id"]
    )
    publish(catalog, first, second["sealed"]["release_id"], action="rollback")
    assert (
        catalog.bind(bind_request(first, "TASK-AFTER-ROLLBACK"))["release_id"]
        == first["sealed"]["release_id"]
    )


def test_switch_and_catalog_insert_roll_back_together(database, release_factory):
    first, second = release_factory("A"), release_factory("B")
    catalog = Catalog(database, first["store"])
    publish(catalog, first)

    def fail():
        raise RuntimeError("SYNTHETIC failure after pointer update")

    req = PublishRequest(
        **{k: second["sealed"][k] for k in ("release_id", "manifest_sha256")},
        expected_default=first["sealed"]["release_id"],
        retain_until=datetime.now(UTC),
    )
    with pytest.raises(RuntimeError):
        catalog.publish(req, after_switch=fail)
    assert current(database) == first["sealed"]["release_id"]
    with database.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(dataset_releases)) == 1


@pytest.mark.parametrize("damage", ["missing_vector_manifest", "artifact_hash"])
def test_failed_candidate_publish_never_changes_default(database, release_factory, damage):
    first, second = release_factory("A"), release_factory("B")
    catalog = Catalog(database, first["store"])
    publish(catalog, first)
    if damage == "missing_vector_manifest":
        entry = next(e for e in second["manifest"]["inventory"] if e["path"].startswith("qdrant/"))
        (second["directory"] / entry["path"]).unlink()
    else:
        (second["directory"] / "components/bm25.json").write_bytes(b"SYNTHETIC broken")
    with pytest.raises((OSError, ValueError)):
        publish(catalog, second, first["sealed"]["release_id"])
    assert current(database) == first["sealed"]["release_id"]


@pytest.mark.parametrize("damage", ["payload", "vehicle", "membership", "lineage", "task_scope"])
def test_database_mapping_mismatch_blocks_bound_queries(
    database, release_factory, tmp_path, damage
):
    bundle = release_factory("A")
    catalog = Catalog(database, bundle["store"])
    publish(catalog, bundle)
    catalog.bind(bind_request(bundle, "TASK-DB-MISMATCH"))
    version = next(iter(bundle["manifest"]["record_hashes"]))
    with database.begin() as connection:
        if damage == "payload":
            connection.execute(
                record_versions.update()
                .where(record_versions.c.version_id == version)
                .values(payload={"SYNTHETIC": "tampered"})
            )
        elif damage == "vehicle":
            connection.execute(
                entity_vehicles.update()
                .where(entity_vehicles.c.version_id == version)
                .values(model_year=2011)
            )
        elif damage == "membership":
            connection.execute(
                release_records.update()
                .where(release_records.c.release_id == bundle["sealed"]["release_id"])
                .values(record_sha256="0" * 64)
            )
        elif damage == "lineage":
            connection.execute(
                snapshot_rows.update()
                .where(snapshot_rows.c.snapshot_id == bundle["snapshot"].snapshot_id)
                .values(raw_pointer="/results/999")
            )
        else:
            connection.execute(
                release_tasks.update()
                .where(release_tasks.c.task_id == "TASK-DB-MISMATCH")
                .values(spec_sha256="0" * 64)
            )
    with pytest.raises(ArtifactIntegrityError):
        service(catalog, tmp_path).query(
            ReleaseQuery(task_id="TASK-DB-MISMATCH", query="brake", method="bm25")
        )


def test_task_binding_cannot_change_scope_or_release(database, release_factory):
    first, second = release_factory("A"), release_factory("B")
    catalog = Catalog(database, first["store"])
    publish(catalog, first)
    catalog.bind(bind_request(first, "TASK-PIN"))
    publish(catalog, second, first["sealed"]["release_id"])
    with pytest.raises(ReleaseConflict):
        catalog.bind(bind_request(second, "TASK-PIN", True))
    with pytest.raises(ReleaseConflict):
        catalog.bind(
            bind_request(first, "TASK-PIN").model_copy(update={"scope": {"model_year": 2011}})
        )
    with pytest.raises(PermissionError):
        catalog.bind(BindTask(task_id="TASK-DENIED", allowed_source_ids=["NOT-ALLOWED"]))


def test_concurrent_publish_cas_has_one_winner_and_no_implicit_overwrite(database, release_factory):
    first, second, third = release_factory("A"), release_factory("B"), release_factory("C")
    catalog = Catalog(database, first["store"])
    publish(catalog, first)
    # Preload shared data before starting the competing final transactions.
    for bundle in (second, third):
        catalog.prepare_database(bundle["manifest"])
    barrier = Barrier(2)

    def attempt(bundle):
        barrier.wait(timeout=10)
        try:
            publish(catalog, bundle, first["sealed"]["release_id"])
            return "published"
        except ReleaseConflict:
            return "cas_rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, [second, third]))
    assert sorted(results) == ["cas_rejected", "published"]


def test_collection_checks_task_reports_retention_and_can_resume_after_failure(
    database, release_factory, tmp_path, monkeypatch
):
    first, second = release_factory("A"), release_factory("B")
    now = [datetime.now(UTC)]
    catalog = Catalog(database, first["store"], clock=lambda: now[0])
    publish(catalog, first)
    catalog.bind(bind_request(first, "TASK-RETENTION"))
    assert "default_release" in catalog.collect(first["sealed"]["release_id"])["reasons"]
    publish(catalog, second, first["sealed"]["release_id"])
    ref = ReportReference(
        reference_id="REPORT-ONE",
        task_id="TASK-RETENTION",
        artifact_sha256="a" * 64,
        keep_until=now[0] + timedelta(days=10),
    )
    catalog.reference(ref)
    reasons = catalog.collect(first["sealed"]["release_id"])["reasons"]
    assert set(reasons) == {"retention_not_elapsed", "active_tasks", "report_references"}
    catalog.finish("TASK-RETENTION")
    now[0] += timedelta(days=2)
    assert catalog.collect(first["sealed"]["release_id"])["reasons"] == ["report_references"]
    now[0] += timedelta(days=20)
    assert catalog.collect(first["sealed"]["release_id"])["status"] == "eligible"
    original = catalog.store.remove_retired_bundle

    def blocked(*args):
        raise OSError("SYNTHETIC interrupted cleanup")

    monkeypatch.setattr(catalog.store, "remove_retired_bundle", blocked)
    with pytest.raises(OSError):
        catalog.collect(first["sealed"]["release_id"], execute=True)
    with pytest.raises(ReleaseUnavailable):
        catalog.bind(bind_request(first, "TASK-TOO-LATE", True))
    monkeypatch.setattr(catalog.store, "remove_retired_bundle", original)
    assert catalog.collect(first["sealed"]["release_id"], execute=True)["status"] == "collected"
    assert not first["directory"].exists() and second["directory"].exists()
    with database.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(record_versions)) == 2


def test_published_http_never_accepts_unbound_or_mixed_release_query(
    database, release_factory, tmp_path
):
    bundle = release_factory("A")
    catalog = Catalog(database, bundle["store"])
    publish(catalog, bundle)
    with TestClient(
        create_release_app(development_mode=True, service=service(catalog, tmp_path))
    ) as client:
        query = {"task_id": "TASK-HTTP", "query": "brake", "method": "bm25"}
        assert client.post("/published/search", json=query).status_code == 409
        assert (
            client.post(
                "/tasks/bind", json=bind_request(bundle, "TASK-HTTP").model_dump(mode="json")
            ).status_code
            == 200
        )
        result = client.post("/published/search", json=query)
        assert (
            result.status_code == 200
            and result.json()["release_id"] == bundle["sealed"]["release_id"]
        )
        assert (
            client.post(
                "/published/search", json=query | {"expected_release_id": "0" * 64}
            ).status_code
            == 409
        )
        hit = result.json()["matched_hits"][0]
        expanded = client.post(
            "/published/expand", json={"request": query, "document_id": hit["document_id"]}
        )
        assert (
            expanded.status_code == 200 and expanded.json()["focus"]["bundle"]["focus"]["citations"]
        )
        assert (
            client.post("/published/search", json=query | {"release_id": "0" * 64}).status_code
            == 422
        )
