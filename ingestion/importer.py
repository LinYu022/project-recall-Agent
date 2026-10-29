"""Idempotent transactional imports; preserve each version and observation link."""

from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from database.schema import record_versions, snapshot_imports, snapshot_rows, source_snapshots
from ingestion.records import MAPPING_VERSION, prepare_import


def import_snapshot(engine: sa.Engine, root: Path, snapshot_id: str) -> dict:
    batch = prepare_import(root, snapshot_id)
    snapshot, report = batch.snapshot, batch.report
    assert snapshot.raw is not None
    manifest = snapshot.model_dump(mode="json")
    with engine.begin() as connection:
        connection.execute(
            insert(source_snapshots)
            .values(
                snapshot_id=snapshot_id,
                source_id=snapshot.source.source_id,
                raw_sha256=snapshot.raw.sha256,
                fetched_at=snapshot.fetched_at,
                manifest=manifest,
            )
            .on_conflict_do_nothing(index_elements=["snapshot_id"])
        )
        saved = connection.scalar(
            sa.select(source_snapshots.c.manifest).where(
                source_snapshots.c.snapshot_id == snapshot_id
            )
        )
        if saved != manifest:
            raise ValueError("snapshot_id already belongs to a different immutable manifest")
        claim = (
            insert(snapshot_imports)
            .values(
                snapshot_id=snapshot_id,
                mapping_version=MAPPING_VERSION,
                input_units=report["input_units"],
                accepted=report["accepted"],
                rejected=report["rejected"],
                report=report,
            )
            .on_conflict_do_nothing(index_elements=["snapshot_id", "mapping_version"])
            .returning(snapshot_imports.c.snapshot_id)
        )
        if connection.scalar(claim) is None:
            previous = connection.scalar(
                sa.select(snapshot_imports.c.report).where(
                    snapshot_imports.c.snapshot_id == snapshot_id,
                    snapshot_imports.c.mapping_version == MAPPING_VERSION,
                )
            )
            if previous != report:
                raise ValueError("mapping changed without a new mapping_version")
            return report | {"status": "already_imported", "new_versions": 0}
        new_versions = 0
        for row in batch.rows:
            if row.version is not None:
                version = row.version
                created = connection.scalar(
                    insert(record_versions)
                    .values(**version)
                    .on_conflict_do_nothing(index_elements=["version_id"])
                    .returning(record_versions.c.version_id)
                )
                new_versions += int(created is not None)
                saved_version = (
                    connection.execute(
                        sa.select(record_versions).where(
                            record_versions.c.version_id == version["version_id"]
                        )
                    )
                    .mappings()
                    .one()
                )
                if dict(saved_version) != version:
                    raise ValueError("version identity collides with different mapped content")
            connection.execute(
                snapshot_rows.insert().values(
                    snapshot_id=snapshot_id,
                    mapping_version=MAPPING_VERSION,
                    ordinal=row.ordinal,
                    raw_pointer=row.pointer,
                    version_id=row.version["version_id"] if row.version else None,
                    error_code=row.error_code,
                )
            )
    return report | {"status": "imported", "new_versions": new_versions}


def first_observed_at(connection: sa.Connection, version_id: str):
    # Derived from every linked receipt, so importing an older receipt later is safe.
    return connection.scalar(
        sa.select(sa.func.min(source_snapshots.c.fetched_at))
        .select_from(
            source_snapshots.join(
                snapshot_rows, source_snapshots.c.snapshot_id == snapshot_rows.c.snapshot_id
            )
        )
        .where(snapshot_rows.c.version_id == version_id)
    )
