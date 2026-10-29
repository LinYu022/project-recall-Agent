"""PostgreSQL is the visibility boundary; files/collections stay unreachable until commit."""

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from database.schema import (
    dataset_releases,
    entity_components,
    entity_projections,
    entity_vehicles,
    record_versions,
    release_defaults,
    release_events,
    release_records,
    release_report_refs,
    release_tasks,
    snapshot_imports,
    snapshot_rows,
    source_snapshots,
)
from entities.normalize import EntityRules, project_record
from entities.store import project_versions
from ingestion.importer import import_snapshot
from ingestion.records import content_hash, prepare_import
from releases.bundle import BundleStore, read_json
from releases.contracts import BindTask, PublishRequest, ReportReference
from runtime.artifacts import ArtifactIntegrityError


class ReleaseConflict(ValueError):
    pass


class ReleaseUnavailable(ValueError):
    pass


class Catalog:
    def __init__(self, engine: sa.Engine, store: BundleStore, *, clock=None):
        self.engine, self.store = engine, store
        self.clock = clock or (lambda: datetime.now(UTC))

    @contextmanager
    def transaction(self, *, shared=False):
        with self.engine.connect().execution_options(
            isolation_level="REPEATABLE READ" if shared else "READ COMMITTED"
        ) as connection:
            with connection.begin():
                name = "pg_advisory_xact_lock_shared" if shared else "pg_advisory_xact_lock"
                connection.execute(
                    sa.text(f"SELECT {name}(hashtextextended(current_schema() || :namespace, 0))"),
                    {"namespace": ":release-catalog"},
                )
                yield connection

    def event(self, connection, release_id, action, payload):
        connection.execute(
            release_events.insert().values(
                event_id=str(uuid4()), release_id=release_id, action=action, payload=payload
            )
        )

    def prepare_database(self, manifest: dict):
        base = self.store.directory(manifest)
        for identifier in manifest["snapshot_ids"]:
            import_snapshot(self.engine, base / "raw", identifier)
        rules = EntityRules.model_validate(read_json(base / "rules/entity.json"))
        project_versions(self.engine, list(manifest["record_hashes"]), rules)

    def verify_database(self, connection, manifest: dict, *, registered=True):
        base = self.store.directory(manifest)
        rules = EntityRules.model_validate(read_json(base / "rules/entity.json"))
        versions = {v["version_id"]: v for v in read_json(base / "mapping.json")["records"]}
        rows = (
            connection.execute(
                sa.select(record_versions).where(record_versions.c.version_id.in_(versions))
            )
            .mappings()
            .all()
        )
        if {r["version_id"]: dict(r) for r in rows} != versions:
            raise ArtifactIntegrityError("database mapped records differ from release")
        projections = (
            connection.execute(
                sa.select(entity_projections).where(
                    entity_projections.c.version_id.in_(versions),
                    entity_projections.c.rules_version == rules.rules_version,
                )
            )
            .mappings()
            .all()
        )
        expected = {key: project_record(value, rules) for key, value in versions.items()}
        if len(projections) != len(expected) or any(
            r["rules_sha256"] != rules.sha256 or r["projection"] != expected[r["version_id"]]
            for r in projections
        ):
            raise ArtifactIntegrityError("database projection differs from release mapping rules")
        vehicles = (
            connection.execute(
                sa.select(entity_vehicles).where(
                    entity_vehicles.c.version_id.in_(versions),
                    entity_vehicles.c.rules_version == rules.rules_version,
                )
            )
            .mappings()
            .all()
        )
        parts = (
            connection.execute(
                sa.select(entity_components).where(
                    entity_components.c.version_id.in_(versions),
                    entity_components.c.rules_version == rules.rules_version,
                )
            )
            .mappings()
            .all()
        )
        expected_vehicles = []
        expected_parts = []
        for key, projection in expected.items():
            for vehicle in projection["vehicles"]:
                expected_vehicles.append(
                    {
                        "version_id": key,
                        "rules_version": rules.rules_version,
                        "position": vehicle["position"],
                        **{
                            k: vehicle[k]["canonical"]
                            for k in ("make", "model", "model_year", "production_date")
                        },
                    }
                )
            for i, part in enumerate(projection["components"]):
                expected_parts.append(
                    {
                        "version_id": key,
                        "rules_version": rules.rules_version,
                        "position": i,
                        "canonical": part["canonical"],
                        "raw_label": part["raw"] if isinstance(part["raw"], str) else None,
                    }
                )

        def order(v):
            return (v["version_id"], v["position"])

        if sorted(map(dict, vehicles), key=order) != sorted(expected_vehicles, key=order) or sorted(
            map(dict, parts), key=order
        ) != sorted(expected_parts, key=order):
            raise ArtifactIntegrityError("database entity rows differ from release projections")
        for identifier in manifest["snapshot_ids"]:
            batch = prepare_import(base / "raw", identifier)
            snapshot = (
                connection.execute(
                    sa.select(source_snapshots).where(source_snapshots.c.snapshot_id == identifier)
                )
                .mappings()
                .one_or_none()
            )
            assert batch.snapshot.raw is not None
            if (
                snapshot is None
                or snapshot["manifest"] != batch.snapshot.model_dump(mode="json")
                or snapshot["raw_sha256"] != batch.snapshot.raw.sha256
                or snapshot["source_id"] != batch.snapshot.source.source_id
                or snapshot["fetched_at"] != batch.snapshot.fetched_at
            ):
                raise ArtifactIntegrityError("database receipt differs from release")
            imported = (
                connection.execute(
                    sa.select(snapshot_imports).where(
                        snapshot_imports.c.snapshot_id == identifier,
                        snapshot_imports.c.mapping_version == "source-record/0.1",
                    )
                )
                .mappings()
                .one_or_none()
            )
            if (
                imported is None
                or imported["report"] != batch.report
                or any(
                    imported[k] != batch.report[k] for k in ("input_units", "accepted", "rejected")
                )
            ):
                raise ArtifactIntegrityError("database import report differs")
            stored = (
                connection.execute(
                    sa.select(snapshot_rows)
                    .where(
                        snapshot_rows.c.snapshot_id == identifier,
                        snapshot_rows.c.mapping_version == "source-record/0.1",
                    )
                    .order_by(snapshot_rows.c.ordinal)
                )
                .mappings()
                .all()
            )
            actual = [
                {
                    "snapshot_id": identifier,
                    "mapping_version": "source-record/0.1",
                    "ordinal": row.ordinal,
                    "raw_pointer": row.pointer,
                    "version_id": row.version["version_id"] if row.version else None,
                    "error_code": row.error_code,
                }
                for row in batch.rows
            ]
            if list(map(dict, stored)) != actual:
                raise ArtifactIntegrityError("database source lineage differs")
        if registered:
            linked = dict(
                connection.execute(
                    sa.select(release_records.c.version_id, release_records.c.record_sha256).where(
                        release_records.c.release_id == manifest["release_id"]
                    )
                ).all()
            )
            if linked != manifest["record_hashes"]:
                raise ArtifactIntegrityError("release database membership differs")

    def _available(self, connection, release_id):
        row = (
            connection.execute(
                sa.select(dataset_releases).where(dataset_releases.c.release_id == release_id)
            )
            .mappings()
            .one_or_none()
        )
        if row is None or row["state"] != "available":
            raise ReleaseUnavailable("complete release is not available")
        manifest = self.store.verify(row["manifest_sha256"], release_id)
        if (
            manifest != row["manifest"]
            or manifest["dataset_build_id"] != row["dataset_build_id"]
            or manifest["bundle_key"] != row["bundle_key"]
        ):
            raise ArtifactIntegrityError("database/file release manifest differs")
        self.verify_database(connection, manifest)
        return row, manifest

    def publish(self, request: PublishRequest, *, after_switch=None) -> dict:
        manifest = self.store.verify(request.manifest_sha256, request.release_id)
        self.prepare_database(manifest)
        with self.transaction() as connection:
            self.store.verify(request.manifest_sha256, request.release_id)
            self.verify_database(connection, manifest, registered=False)
            connection.execute(
                insert(release_defaults)
                .values(slot="default", release_id=None)
                .on_conflict_do_nothing()
            )
            current = connection.scalar(
                sa.select(release_defaults.c.release_id).where(release_defaults.c.slot == "default")
            )
            if current != request.expected_default:
                raise ReleaseConflict("default changed; compare-and-swap rejected")
            existing = (
                connection.execute(
                    sa.select(dataset_releases).where(
                        dataset_releases.c.release_id == request.release_id
                    )
                )
                .mappings()
                .one_or_none()
            )
            minimum = self.clock() + timedelta(days=manifest["retention_days"])
            if existing is None:
                if request.action == "rollback":
                    raise ReleaseUnavailable("rollback requires a previously registered release")
                connection.execute(
                    dataset_releases.insert().values(
                        release_id=request.release_id,
                        dataset_build_id=manifest["dataset_build_id"],
                        bundle_key=manifest["bundle_key"],
                        manifest_sha256=request.manifest_sha256,
                        manifest=manifest,
                        retain_until=max(request.retain_until, minimum),
                    )
                )
                connection.execute(
                    release_records.insert(),
                    [
                        {
                            "release_id": request.release_id,
                            "version_id": key,
                            "record_sha256": value,
                        }
                        for key, value in manifest["record_hashes"].items()
                    ],
                )
            elif (
                existing["manifest_sha256"] != request.manifest_sha256
                or existing["manifest"] != manifest
                or existing["state"] != "available"
            ):
                raise ReleaseConflict("registered release identity or state differs")
            else:
                connection.execute(
                    dataset_releases.update()
                    .where(dataset_releases.c.release_id == request.release_id)
                    .values(
                        retain_until=max(existing["retain_until"], request.retain_until, minimum)
                    )
                )
            self.verify_database(connection, manifest)
            connection.execute(
                release_defaults.update()
                .where(release_defaults.c.slot == "default")
                .values(release_id=request.release_id)
            )
            self.event(
                connection, request.release_id, request.action, {"previous_default": current}
            )
            if after_switch is not None:
                after_switch()
        return {
            "status": "published" if request.action == "publish" else "rolled_back",
            "release_id": request.release_id,
            "previous_default": current,
        }

    def bind(self, request: BindTask):
        with self.transaction() as connection:
            from control.access import gate

            gate(connection, request.task_id, "bind", request=request)
            saved = (
                connection.execute(
                    sa.select(release_tasks).where(release_tasks.c.task_id == request.task_id)
                )
                .mappings()
                .one_or_none()
            )
            release_id = request.release_id or (
                saved["release_id"]
                if saved
                else connection.scalar(
                    sa.select(release_defaults.c.release_id).where(
                        release_defaults.c.slot == "default"
                    )
                )
            )
            if release_id is None:
                raise ReleaseUnavailable("no default published release")
            _, manifest = self._available(connection, release_id)
            if not set(request.allowed_source_ids) <= set(manifest["allowed_source_ids"]):
                raise PermissionError("task sources outside release")
            spec = request.model_dump(mode="json") | {"release_id": release_id}
            if saved is not None:
                if saved["spec"] != spec or saved["spec_sha256"] != content_hash(spec):
                    raise ReleaseConflict("task release/scope cannot be rebound")
                return dict(saved)
            connection.execute(
                release_tasks.insert().values(
                    task_id=request.task_id,
                    release_id=release_id,
                    spec=spec,
                    spec_sha256=content_hash(spec),
                )
            )
            self.event(connection, release_id, "task_bound", {"task_id": request.task_id})
            return dict(
                connection.execute(
                    sa.select(release_tasks).where(release_tasks.c.task_id == request.task_id)
                )
                .mappings()
                .one()
            )

    def task(self, connection, task_id, expected=None):
        from control.access import gate

        gate(connection, task_id, "research")
        task = (
            connection.execute(sa.select(release_tasks).where(release_tasks.c.task_id == task_id))
            .mappings()
            .one_or_none()
        )
        if task is None or task["state"] != "active":
            raise ReleaseUnavailable("task has no active immutable release binding")
        if (
            task["spec_sha256"] != content_hash(task["spec"])
            or task["spec"]["release_id"] != task["release_id"]
        ):
            raise ArtifactIntegrityError("task binding identity differs")
        if expected is not None and expected != task["release_id"]:
            raise ReleaseConflict("query release differs from task binding")
        release_row, manifest = self._available(connection, task["release_id"])
        return dict(task) | {"manifest_sha256": release_row["manifest_sha256"]}, manifest

    def finish(self, task_id: str, state="finished"):
        if state not in {"finished", "cancelled"}:
            raise ValueError("invalid task finish state")
        with self.transaction() as connection:
            from control.access import gate

            gate(connection, task_id, "close")
            task = (
                connection.execute(
                    sa.select(release_tasks).where(release_tasks.c.task_id == task_id)
                )
                .mappings()
                .one()
            )
            if task["state"] not in {"active", state}:
                raise ReleaseConflict("task already ended differently")
            connection.execute(
                release_tasks.update().where(release_tasks.c.task_id == task_id).values(state=state)
            )
            self.event(
                connection, task["release_id"], "task_ended", {"task_id": task_id, "state": state}
            )

    def reference(self, request: ReportReference):
        with self.transaction() as connection:
            from control.access import gate

            gate(connection, request.task_id, "generate")
            task = (
                connection.execute(
                    sa.select(release_tasks).where(release_tasks.c.task_id == request.task_id)
                )
                .mappings()
                .one()
            )
            row = (
                connection.execute(
                    sa.select(dataset_releases).where(
                        dataset_releases.c.release_id == task["release_id"]
                    )
                )
                .mappings()
                .one()
            )
            if row["state"] != "available":
                raise ReleaseUnavailable("cannot pin retiring/collected release")
            values = request.model_dump(mode="python") | {"release_id": task["release_id"]}
            connection.execute(
                insert(release_report_refs).values(**values).on_conflict_do_nothing()
            )
            saved = dict(
                connection.execute(
                    sa.select(release_report_refs).where(
                        release_report_refs.c.reference_id == request.reference_id
                    )
                )
                .mappings()
                .one()
            )
            if saved != values:
                raise ReleaseConflict("report reference is immutable")

    def collect(self, release_id: str, *, execute=False) -> dict:
        with self.transaction() as connection:
            row = (
                connection.execute(
                    sa.select(dataset_releases).where(dataset_releases.c.release_id == release_id)
                )
                .mappings()
                .one()
            )
            reasons = []
            if row["state"] == "collected":
                return {"status": "already_collected", "release_id": release_id}
            if (
                connection.scalar(
                    sa.select(release_defaults.c.release_id).where(
                        release_defaults.c.slot == "default"
                    )
                )
                == release_id
            ):
                reasons.append("default_release")
            now = self.clock()
            if row["retain_until"] > now:
                reasons.append("retention_not_elapsed")
            from control.queue_schema import calls as dispatch_calls
            from control.queue_schema import jobs as dispatch_jobs

            if connection.scalar(
                sa.select(sa.func.count())
                .select_from(dispatch_jobs)
                .where(
                    dispatch_jobs.c.pinned["release_id"].astext == release_id,
                    sa.or_(
                        dispatch_jobs.c.state.in_(["queued", "running", "waiting_input"]),
                        sa.exists(
                            sa.select(dispatch_calls.c.call_id).where(
                                dispatch_calls.c.job_id == dispatch_jobs.c.job_id,
                                sa.or_(
                                    dispatch_calls.c.pending_reconciliation.is_(True),
                                    dispatch_calls.c.state == "reserved",
                                ),
                            )
                        ),
                    ),
                )
            ):
                reasons.append("active_dispatch_jobs")
            if connection.scalar(
                sa.select(sa.func.count())
                .select_from(release_tasks)
                .where(release_tasks.c.release_id == release_id, release_tasks.c.state == "active")
            ):
                reasons.append("active_tasks")
            if connection.scalar(
                sa.select(sa.func.count())
                .select_from(release_report_refs)
                .where(
                    release_report_refs.c.release_id == release_id,
                    sa.or_(
                        release_report_refs.c.keep_until.is_(None),
                        release_report_refs.c.keep_until > now,
                    ),
                )
            ):
                reasons.append("report_references")
            if reasons:
                return {"status": "retained", "release_id": release_id, "reasons": reasons}
            if not execute:
                return {"status": "eligible", "release_id": release_id}
            connection.execute(
                dataset_releases.update()
                .where(dataset_releases.c.release_id == release_id)
                .values(state="retiring")
            )
            self.event(connection, release_id, "retiring", {})
            manifest = row["manifest"]
        with self.transaction() as connection:
            state = connection.scalar(
                sa.select(dataset_releases.c.state).where(
                    dataset_releases.c.release_id == release_id
                )
            )
            if state == "collected":
                return {"status": "already_collected", "release_id": release_id}
            if state != "retiring":
                raise ReleaseConflict("collection state changed")
            if self.store.manifests.read_json(row["manifest_sha256"]) != manifest:
                raise ArtifactIntegrityError("registered retirement manifest differs")
            self.store.remove_retired_bundle(manifest)
            connection.execute(
                dataset_releases.update()
                .where(
                    dataset_releases.c.release_id == release_id,
                    dataset_releases.c.state == "retiring",
                )
                .values(state="collected")
            )
            self.event(connection, release_id, "collected", {})
        return {
            "status": "collected",
            "release_id": release_id,
            "shared_source_database_rows": "retained",
        }
